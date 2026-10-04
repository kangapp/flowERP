"""L08 personal contract checks against the actual sales service and SQLite state."""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import uuid

import flowerp.sales
from flowerp import MasterDataService
from flowerp.identity import IdentityService, SYSTEM_PRINCIPAL
from flowerp.inventory import InventoryService
from flowerp.models import InsufficientStock
from flowerp.sales import SalesService
from flowerp.store import ERPStore


TABLES = ('stock_balance', 'stock_reservations', 'sales_document_lines', 'sales_documents')


def _check(mode):
    root = Path.cwd().resolve()
    module = Path(flowerp.sales.__file__).resolve()
    assert module.is_relative_to(root), f'Wrong product module: {module}'
    with tempfile.TemporaryDirectory(prefix='l08-personal-') as temporary:
        store = ERPStore(Path(temporary) / 'orders.db')
        IdentityService(store).ensure_local_defaults()
        master, inventory, sales = MasterDataService(store), InventoryService(store), SalesService(store)
        actor = SYSTEM_PRINCIPAL
        products = [master.create_product(actor, 'L08-PERSONAL-' + sku, sku, 1000, 500)
                    for sku in ('A', 'B')]
        customer = master.create_customer(actor, 'L08-PERSONAL-C', 'L08 contract', credit_limit_cents=100000)
        for index, product in enumerate(products):
            quantity = 2 if mode == 'shortage' and index == 1 else 5
            inventory.receive(actor, product['id'], 'LOC-MAIN-STOCK', quantity, f'l08-opening-{index}')
        order = sales.create_order(actor, customer['id'], [
            {'product_id': products[0]['id'], 'quantity': 2},
            {'product_id': products[1]['id'], 'quantity': 3}])
        sales.confirm(actor, order['id'])

        def snapshot():
            return {table: store.rows(f'SELECT * FROM {table} ORDER BY rowid') for table in TABLES}

        before = snapshot()
        if mode == 'write-error':
            product_id = products[1]['id'].replace("'", "''")
            with store.connect() as connection:
                connection.execute(
                    'CREATE TRIGGER l08_personal_second_write BEFORE INSERT ON stock_reservations '
                    f"WHEN NEW.product_id='{product_id}' BEGIN "
                    "SELECT RAISE(ABORT, 'L08 personal second-write failure'); END")
        error = None
        try:
            sales.reserve(actor, order['id'])
        except (InsufficientStock, sqlite3.IntegrityError) as exception:
            error = {'type': type(exception).__name__, 'message': str(exception)}
        after = snapshot()
        balances = [inventory.balance(actor, product['id'], 'LOC-MAIN-STOCK') for product in products]
        current = sales.order(actor, order['id'])
        changed = [table for table in TABLES if before[table] != after[table]]
        observation = {'mode': mode, 'actual_module': str(module), 'interpreter': sys.executable,
                       'actual_cwd': str(root), 'evidence_kind': 'actual_candidate_temporary_sqlite',
                       'before': before, 'after': after, 'changed_tables': changed,
                       'reserved': [balance['reserved'] for balance in balances],
                       'available': [balance['available'] for balance in balances],
                       'order_status': current['status'], 'error': error, 'real_remote_run': False}
        output = root / '.runtime/l08-observations' / (mode + '-' + uuid.uuid4().hex + '.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(observation, ensure_ascii=False, indent=2) + '\n')
        reason = f'four-table observation: {output}; changed={changed}; reserved={observation["reserved"]}'
        if mode == 'success':
            assert error is None and current['status'] == 'reserved', reason
            assert observation['reserved'] == [2, 3] and observation['available'] == [3, 2], reason
            assert len(after['stock_reservations']) == 2, reason
            assert [line['reserved_quantity'] for line in after['sales_document_lines']] == [2, 3], reason
            assert [balance['on_hand'] for balance in balances] == [5, 5], reason
        else:
            expected_type = 'InsufficientStock' if mode == 'shortage' else 'IntegrityError'
            assert error is not None and error['type'] == expected_type, reason
            if mode == 'write-error':
                assert 'L08 personal second-write failure' in error['message'], reason
            assert before == after and current['status'] == 'confirmed', reason
            assert observation['reserved'] == [0, 0], reason
            assert observation['available'] == ([5, 2] if mode == 'shortage' else [5, 5]), reason
        return json.dumps({'mode': mode, 'observation': str(output), 'actual_module': str(module),
                           'reserved': observation['reserved'], 'available': observation['available'],
                           'four_tables_unchanged': before == after, 'order_status': current['status']},
                          ensure_ascii=False)


def l08_personal_atomic_success():
    return _check('success')


def l08_personal_atomic_shortage():
    return _check('shortage')


def l08_personal_atomic_write_error():
    return _check('write-error')
