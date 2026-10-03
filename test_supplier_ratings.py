"""Ratings API validates atomically and respects manager/branch boundaries."""
import uuid

import pytest

from app import app, db, Branch, Supplier, User, get_default_branch_id


@pytest.fixture
def supplier_client():
    with app.app_context():
        branch = db.session.get(Branch, get_default_branch_id())
        user = User.query.filter_by(username='admin').first()
        supplier = Supplier(name=f'Rating test {uuid.uuid4().hex}', branch_id=branch.id,
                            quality_rating=3, delivery_rating=2)
        db.session.add(supplier)
        db.session.commit()
        supplier_id = supplier.id
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
            session['role'] = 'manager'
            session['branch_id'] = branch.id
        try:
            yield client, supplier_id
        finally:
            db.session.rollback()
            Supplier.query.filter_by(id=supplier_id).delete()
            db.session.commit()


def test_save_and_clear_ratings(supplier_client):
    client, supplier_id = supplier_client
    response = client.post(f'/api/suppliers/{supplier_id}/ratings',
                           json={'quality_rating': 4.5, 'delivery_rating': 0})
    assert response.status_code == 200
    supplier = db.session.get(Supplier, supplier_id)
    db.session.refresh(supplier)
    assert (supplier.quality_rating, supplier.delivery_rating) == (4.5, 0)


@pytest.mark.parametrize('value', [-1, 6, 'NaN', 'Infinity', 'bad', None, True])
def test_invalid_rating_does_not_save_either_score(supplier_client, value):
    client, supplier_id = supplier_client
    response = client.post(f'/api/suppliers/{supplier_id}/ratings',
                           json={'quality_rating': 5, 'delivery_rating': value})
    assert response.status_code == 400
    supplier = db.session.get(Supplier, supplier_id)
    db.session.refresh(supplier)
    assert (supplier.quality_rating, supplier.delivery_rating) == (3, 2)


def test_other_branch_cannot_rate_supplier(supplier_client):
    client, supplier_id = supplier_client
    branch = Branch(name=f'Rating branch {uuid.uuid4().hex}', code=uuid.uuid4().hex[:10],
                    is_active=True, is_default=False)
    db.session.add(branch)
    db.session.flush()
    supplier = db.session.get(Supplier, supplier_id)
    original_branch = supplier.branch_id
    supplier.branch_id = branch.id
    db.session.commit()
    try:
        response = client.post(f'/api/suppliers/{supplier_id}/ratings', json={'quality_rating': 5})
        assert response.status_code == 404
    finally:
        supplier.branch_id = original_branch
        db.session.delete(branch)
        db.session.commit()


def test_cashier_cannot_rate_supplier(supplier_client):
    client, supplier_id = supplier_client
    with client.session_transaction() as session:
        session['role'] = 'cashier'
    assert client.post(f'/api/suppliers/{supplier_id}/ratings',
                       json={'quality_rating': 5}).status_code == 403


@pytest.mark.parametrize('payload', [{}, [], 'bad'])
def test_invalid_payload_rejected(supplier_client, payload):
    client, supplier_id = supplier_client
    assert client.post(f'/api/suppliers/{supplier_id}/ratings', json=payload).status_code == 400