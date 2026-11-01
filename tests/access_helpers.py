def employee(store, telegram_id=1, name='Иван'):
    with store.atomic():
        store.db.execute('INSERT INTO employees(name,phone,telegram_id) VALUES (?,?,?)', (name, f'+1202555{100 + telegram_id % 100:04d}', telegram_id))
