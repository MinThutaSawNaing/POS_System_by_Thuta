"""Manager-controlled, branch-scoped automatic offline fetch preference."""

import uuid

from app import AppSetting, Branch, User, app, branch_setting_key, db


def test_automated_offline_fetch_settings():
    app.config.update(TESTING=True)
    with app.app_context():
        user = User.query.filter_by(username='admin').first()
        assert user is not None
        branch = Branch(name='Offline preference test', code='AUTO-' + uuid.uuid4().hex[:8], is_active=True)
        db.session.add(branch)
        db.session.commit()
        key = branch_setting_key(branch.id, 'automated_offline_fetch')
        client = app.test_client()
        try:
            with client.session_transaction() as session:
                session.update(user_id=user.id, role='manager', branch_id=branch.id)
            assert client.get('/api/settings').get_json()['automated_offline_fetch'] is False
            for invalid in ('on', 1, None, []):
                response = client.put('/api/settings', json={'branch_id': branch.id, 'automated_offline_fetch': invalid})
                assert response.status_code == 400
                assert AppSetting.query.filter_by(key=key).first() is None
            response = client.put('/api/settings', json={'branch_id': branch.id, 'automated_offline_fetch': True})
            assert response.status_code == 200
            assert response.get_json()['automated_offline_fetch'] is True
            assert client.get('/api/settings').get_json()['automated_offline_fetch'] is True
            with client.session_transaction() as session:
                session['role'] = 'cashier'
            assert client.get('/api/settings').get_json()['automated_offline_fetch'] is True
            assert client.put('/api/settings', json={'automated_offline_fetch': False}).status_code == 403
            with client.session_transaction() as session:
                session['role'] = 'manager'
            assert client.put('/api/settings', json={'branch_id': -1, 'automated_offline_fetch': False}).status_code == 409
            response = client.put('/api/settings', json={'branch_id': branch.id, 'automated_offline_fetch': False})
            assert response.get_json()['automated_offline_fetch'] is False
        finally:
            setting = AppSetting.query.filter_by(key=key).first()
            if setting:
                db.session.delete(setting)
            db.session.delete(branch)
            db.session.commit()