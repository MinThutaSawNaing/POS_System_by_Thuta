"""
AI Tools Module for POS System
Defines all tools the AI Agent can use for inventory and procurement tasks
"""

import json
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from decimal import Decimal


# Tool schema definitions for the AI
TOOL_SCHEMAS = {
    "get_inventory_status": {
        "name": "get_inventory_status",
        "description": "Get the current inventory status for all products or a specific product. Returns stock levels, reorder points, and stock status.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "Optional product ID to get status for a specific product. If not provided, returns all products."
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter to get products in a specific category."
                },
                "low_stock_only": {
                    "type": "boolean",
                    "description": "If true, only returns products with stock at or below reorder point."
                }
            }
        }
    },
    "get_low_stock_items": {
        "name": "get_low_stock_items",
        "description": "Get a list of all products that are low on stock (at or below reorder point) or out of stock. Includes suggested reorder quantities.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    "get_supplier_list": {
        "name": "get_supplier_list",
        "description": "Get a list of all suppliers with their details including contact info, ratings, and performance metrics.",
        "parameters": {
            "type": "object",
            "properties": {
                "active_only": {
                    "type": "boolean",
                    "description": "If true, only returns active suppliers."
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter for suppliers."
                }
            }
        }
    },
    "get_supplier_details": {
        "name": "get_supplier_details",
        "description": "Get detailed information about a specific supplier including their price agreements and order history.",
        "parameters": {
            "type": "object",
            "properties": {
                "supplier_id": {
                    "type": "integer",
                    "description": "The ID of the supplier to get details for."
                }
            },
            "required": ["supplier_id"]
        }
    },
    "get_purchase_orders": {
        "name": "get_purchase_orders",
        "description": "Get a list of purchase orders with optional filtering by status, supplier, or date range.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: draft, pending, approved, partially_received, received, cancelled"
                },
                "supplier_id": {
                    "type": "integer",
                    "description": "Filter by supplier ID."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of orders to return. Default is 50."
                }
            }
        }
    },
    "create_purchase_order": {
        "name": "create_purchase_order",
        "description": "Create a new purchase order for one or more products. Automatically calculates totals and generates PO number.",
        "parameters": {
            "type": "object",
            "properties": {
                "supplier_id": {
                    "type": "integer",
                    "description": "The ID of the supplier to order from."
                },
                "items": {
                    "type": "array",
                    "description": "List of items to order. Each item should have product_id, quantity, and optionally unit_cost.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "quantity": {"type": "integer"},
                            "unit_cost": {"type": "number"}
                        },
                        "required": ["product_id", "quantity"]
                    }
                },
                "expected_delivery_date": {
                    "type": "string",
                    "description": "Expected delivery date in YYYY-MM-DD format."
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes for the purchase order."
                }
            },
            "required": ["supplier_id", "items"]
        }
    },
    "approve_purchase_order": {
        "name": "approve_purchase_order",
        "description": "Approve a pending purchase order. Changes status from 'pending' to 'approved'.",
        "parameters": {
            "type": "object",
            "properties": {
                "po_id": {
                    "type": "integer",
                    "description": "The ID of the purchase order to approve."
                }
            },
            "required": ["po_id"]
        }
    },
    "cancel_purchase_order": {
        "name": "cancel_purchase_order",
        "description": "Cancel a purchase order. Can only cancel orders in draft, pending, or approved status.",
        "parameters": {
            "type": "object",
            "properties": {
                "po_id": {
                    "type": "integer",
                    "description": "The ID of the purchase order to cancel."
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for cancellation."
                }
            },
            "required": ["po_id", "reason"]
        }
    },
    "get_warehouse_inventory": {
        "name": "get_warehouse_inventory",
        "description": "Get the current warehouse inventory status. Shows products received but not yet transferred to main stock.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "Optional product ID to filter by specific product."
                }
            }
        }
    },
    "create_warehouse_transfer": {
        "name": "create_warehouse_transfer",
        "description": "Transfer products from warehouse inventory to main product stock. Reduces warehouse quantity and increases main stock.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "The ID of the product to transfer."
                },
                "quantity": {
                    "type": "integer",
                    "description": "The quantity to transfer from warehouse to main stock."
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes for the transfer."
                }
            },
            "required": ["product_id", "quantity"]
        }
    },
    "get_sales_trends": {
        "name": "get_sales_trends",
        "description": "Get sales trend analysis for products over a specified time period. Useful for making reorder decisions.",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to analyze. Default is 30."
                },
                "product_id": {
                    "type": "integer",
                    "description": "Optional product ID to get trends for a specific product."
                },
                "top_n": {
                    "type": "integer",
                    "description": "Return top N best selling products. Default is 10."
                }
            }
        }
    },
    "get_product_details": {
        "name": "get_product_details",
        "description": "Get detailed information about a specific product including stock, pricing, and supplier information.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "The ID of the product."
                },
                "barcode": {
                    "type": "string",
                    "description": "Alternative: the barcode of the product."
                }
            }
        }
    },
    "suggest_reorder_quantities": {
        "name": "suggest_reorder_quantities",
        "description": "Analyze inventory and sales trends to suggest optimal reorder quantities for low stock items.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    "get_supplier_price_for_product": {
        "name": "get_supplier_price_for_product",
        "description": "Get the agreed price for a product from a specific supplier, if a price agreement exists.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "The ID of the product."
                },
                "supplier_id": {
                    "type": "integer",
                    "description": "The ID of the supplier."
                }
            },
            "required": ["product_id", "supplier_id"]
        }
    }
}


class AITools:
    """Container for all AI tool functions with database access"""
    
    def __init__(self, db, models):
        self.db = db
        self.models = models
        
    def _get_model(self, name):
        """Get a model class by name"""
        return self.models.get(name)
        
    def get_inventory_status(self, product_id: int = None, category: str = None, 
                            low_stock_only: bool = False) -> Dict[str, Any]:
        """Get current inventory status"""
        Product = self._get_model('Product')
        query = Product.query
        
        if product_id:
            query = query.filter_by(id=product_id)
        if category:
            query = query.filter_by(category=category)
            
        products = query.all()
        result = []
        
        for product in products:
            current_stock = int(product.stock or 0)
            reorder_point = max(int(product.reorder_point or 0), 0)
            reorder_enabled = bool(product.reorder_enabled)
            
            is_low = reorder_enabled and current_stock <= reorder_point
            is_out = current_stock <= 0
            
            if low_stock_only and not (is_low or is_out):
                continue
                
            result.append({
                "product_id": product.id,
                "name": product.name,
                "barcode": product.barcode,
                "category": product.category,
                "current_stock": current_stock,
                "reorder_point": reorder_point,
                "reorder_quantity": max(int(product.reorder_quantity or 0), 0),
                "reorder_enabled": reorder_enabled,
                "status": "out_of_stock" if is_out else ("low_stock" if is_low else "ok"),
                "price": float(product.price or 0),
                "cost": float(product.cost or 0)
            })
            
        return {
            "total_products": len(result),
            "inventory": result
        }
        
    def get_low_stock_items(self) -> Dict[str, Any]:
        """Get all low stock items with suggested reorder quantities"""
        Product = self._get_model('Product')
        products = Product.query.filter_by(reorder_enabled=True).all()
        
        low_stock_items = []
        out_of_stock_count = 0
        
        for product in products:
            current_stock = int(product.stock or 0)
            reorder_point = max(int(product.reorder_point or 0), 0)
            reorder_quantity = max(int(product.reorder_quantity or 0), 0)
            
            if current_stock <= 0:
                out_of_stock_count += 1
                
            if current_stock <= reorder_point:
                suggested_qty = reorder_quantity if reorder_quantity > 0 else max(reorder_point - current_stock, 1)
                low_stock_items.append({
                    "product_id": product.id,
                    "name": product.name,
                    "barcode": product.barcode,
                    "category": product.category,
                    "current_stock": current_stock,
                    "reorder_point": reorder_point,
                    "suggested_reorder_qty": suggested_qty,
                    "unit_cost": float(product.cost or 0),
                    "estimated_cost": round(suggested_qty * float(product.cost or 0), 2)
                })
                
        return {
            "summary": {
                "low_stock_count": len(low_stock_items),
                "out_of_stock_count": out_of_stock_count
            },
            "items": low_stock_items
        }
        
    def get_supplier_list(self, active_only: bool = True, category: str = None) -> Dict[str, Any]:
        """Get list of suppliers"""
        Supplier = self._get_model('Supplier')
        query = Supplier.query
        
        if active_only:
            query = query.filter_by(is_active=True)
        if category:
            query = query.filter_by(category=category)
            
        suppliers = query.all()
        result = []
        
        for supplier in suppliers:
            result.append({
                "supplier_id": supplier.id,
                "name": supplier.name,
                "contact_person": supplier.contact_person,
                "phone": supplier.phone,
                "email": supplier.email,
                "category": supplier.category,
                "payment_terms": supplier.payment_terms,
                "lead_time_days": supplier.lead_time_days,
                "quality_rating": float(supplier.quality_rating or 0),
                "delivery_rating": float(supplier.delivery_rating or 0),
                "total_orders": supplier.total_orders or 0,
                "is_active": supplier.is_active
            })
            
        return {
            "total_suppliers": len(result),
            "suppliers": result
        }
        
    def get_supplier_details(self, supplier_id: int) -> Dict[str, Any]:
        """Get detailed supplier information"""
        Supplier = self._get_model('Supplier')
        SupplierPriceAgreement = self._get_model('SupplierPriceAgreement')
        PurchaseOrder = self._get_model('PurchaseOrder')
        
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {"error": f"Supplier with ID {supplier_id} not found"}
            
        # Get price agreements
        price_agreements = []
        for pa in supplier.price_agreements:
            price_agreements.append({
                "product_id": pa.product_id,
                "product_name": pa.product.name if pa.product else None,
                "agreed_price": float(pa.agreed_price),
                "valid_from": pa.valid_from.isoformat() if pa.valid_from else None,
                "valid_to": pa.valid_to.isoformat() if pa.valid_to else None
            })
            
        # Get recent purchase orders
        recent_pos = []
        for po in supplier.purchase_orders[-10:]:  # Last 10 orders
            recent_pos.append({
                "po_id": po.id,
                "po_number": po.po_number,
                "status": po.status,
                "total_amount": float(po.total_amount or 0),
                "created_at": po.created_at.isoformat() if po.created_at else None
            })
            
        return {
            "supplier_id": supplier.id,
            "name": supplier.name,
            "contact_person": supplier.contact_person,
            "phone": supplier.phone,
            "email": supplier.email,
            "address": supplier.address,
            "category": supplier.category,
            "payment_terms": supplier.payment_terms,
            "lead_time_days": supplier.lead_time_days,
            "bank_name": supplier.bank_name,
            "bank_account": supplier.bank_account,
            "quality_rating": float(supplier.quality_rating or 0),
            "delivery_rating": float(supplier.delivery_rating or 0),
            "total_orders": supplier.total_orders or 0,
            "on_time_deliveries": supplier.on_time_deliveries or 0,
            "price_agreements": price_agreements,
            "recent_purchase_orders": recent_pos
        }
        
    def get_purchase_orders(self, status: str = None, supplier_id: int = None, 
                           limit: int = 50) -> Dict[str, Any]:
        """Get purchase orders with optional filtering"""
        PurchaseOrder = self._get_model('PurchaseOrder')
        query = PurchaseOrder.query
        
        if status:
            query = query.filter_by(status=status)
        if supplier_id:
            query = query.filter_by(supplier_id=supplier_id)
            
        orders = query.order_by(PurchaseOrder.created_at.desc()).limit(limit).all()
        result = []
        
        for po in orders:
            items = []
            for item in po.items:
                items.append({
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else None,
                    "ordered_qty": item.ordered_qty,
                    "received_qty": item.received_qty,
                    "unit_cost": float(item.unit_cost or 0)
                })
                
            result.append({
                "po_id": po.id,
                "po_number": po.po_number,
                "supplier_id": po.supplier_id,
                "supplier_name": po.supplier.name if po.supplier else None,
                "status": po.status,
                "total_amount": float(po.total_amount or 0),
                "expected_delivery_date": po.expected_delivery_date.isoformat() if po.expected_delivery_date else None,
                "created_at": po.created_at.isoformat() if po.created_at else None,
                "items": items
            })
            
        return {
            "total_orders": len(result),
            "orders": result
        }
        
    def create_purchase_order(self, supplier_id: int, items: List[Dict], 
                             expected_delivery_date: str = None, notes: str = None) -> Dict[str, Any]:
        """Create a new purchase order"""
        Supplier = self._get_model('Supplier')
        Product = self._get_model('Product')
        PurchaseOrder = self._get_model('PurchaseOrder')
        PurchaseOrderItem = self._get_model('PurchaseOrderItem')
        
        # Validate supplier
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {"error": f"Supplier with ID {supplier_id} not found"}
            
        # Generate PO number
        po_number = f"PO-{datetime.now().strftime('%Y%m%d')}-{self._generate_random_suffix()}"
        
        # Create PO
        po = PurchaseOrder(
            po_number=po_number,
            supplier_id=supplier_id,
            status='draft',
            notes=notes or '',
            expected_delivery_date=datetime.strptime(expected_delivery_date, '%Y-%m-%d') if expected_delivery_date else None
        )
        self.db.session.add(po)
        self.db.session.flush()  # Get PO ID
        
        total_amount = 0
        created_items = []
        
        for item_data in items:
            product_id = item_data.get('product_id')
            quantity = item_data.get('quantity')
            unit_cost = item_data.get('unit_cost')
            
            product = Product.query.get(product_id)
            if not product:
                self.db.session.rollback()
                return {"error": f"Product with ID {product_id} not found"}
                
            # Use product cost if unit_cost not provided
            if unit_cost is None:
                unit_cost = product.cost or 0
                
            po_item = PurchaseOrderItem(
                purchase_order_id=po.id,
                product_id=product_id,
                ordered_qty=quantity,
                unit_cost=unit_cost
            )
            self.db.session.add(po_item)
            total_amount += quantity * unit_cost
            
            created_items.append({
                "product_id": product_id,
                "product_name": product.name,
                "quantity": quantity,
                "unit_cost": float(unit_cost)
            })
            
        po.total_amount = total_amount
        self.db.session.commit()
        
        return {
            "success": True,
            "po_id": po.id,
            "po_number": po_number,
            "supplier_name": supplier.name,
            "total_amount": round(total_amount, 2),
            "status": "draft",
            "items_count": len(created_items),
            "items": created_items
        }
        
    def approve_purchase_order(self, po_id: int) -> Dict[str, Any]:
        """Approve a purchase order"""
        PurchaseOrder = self._get_model('PurchaseOrder')
        
        po = PurchaseOrder.query.get(po_id)
        if not po:
            return {"error": f"Purchase order with ID {po_id} not found"}
            
        if po.status != 'pending':
            return {"error": f"Cannot approve purchase order with status '{po.status}'. Only 'pending' orders can be approved."}
            
        po.status = 'approved'
        po.approved_at = datetime.utcnow()
        self.db.session.commit()
        
        return {
            "success": True,
            "po_id": po.id,
            "po_number": po.po_number,
            "status": "approved",
            "message": f"Purchase order {po.po_number} has been approved."
        }
        
    def cancel_purchase_order(self, po_id: int, reason: str) -> Dict[str, Any]:
        """Cancel a purchase order"""
        PurchaseOrder = self._get_model('PurchaseOrder')
        
        po = PurchaseOrder.query.get(po_id)
        if not po:
            return {"error": f"Purchase order with ID {po_id} not found"}
            
        if po.status in ['received', 'cancelled']:
            return {"error": f"Cannot cancel purchase order with status '{po.status}'."}
            
        po.status = 'cancelled'
        po.cancelled_at = datetime.utcnow()
        po.cancelled_reason = reason
        self.db.session.commit()
        
        return {
            "success": True,
            "po_id": po.id,
            "po_number": po.po_number,
            "status": "cancelled",
            "reason": reason,
            "message": f"Purchase order {po.po_number} has been cancelled."
        }
        
    def get_warehouse_inventory(self, product_id: int = None) -> Dict[str, Any]:
        """Get warehouse inventory status"""
        WarehouseInventory = self._get_model('WarehouseInventory')
        query = WarehouseInventory.query
        
        if product_id:
            query = query.filter_by(product_id=product_id)
            
        items = query.all()
        result = []
        
        for item in items:
            if item.quantity > 0:  # Only show items with stock
                result.append({
                    "warehouse_item_id": item.id,
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else None,
                    "barcode": item.product.barcode if item.product else None,
                    "quantity": item.quantity,
                    "location": item.location,
                    "batch_number": item.batch_number,
                    "received_date": item.received_date.isoformat() if item.received_date else None,
                    "unit_cost": float(item.unit_cost or 0)
                })
                
        return {
            "total_items": len(result),
            "warehouse_items": result
        }
        
    def create_warehouse_transfer(self, product_id: int, quantity: int, notes: str = None) -> Dict[str, Any]:
        """Transfer products from warehouse to main stock"""
        WarehouseInventory = self._get_model('WarehouseInventory')
        WarehouseTransfer = self._get_model('WarehouseTransfer')
        Product = self._get_model('Product')
        
        product = Product.query.get(product_id)
        if not product:
            return {"error": f"Product with ID {product_id} not found"}
            
        # Find warehouse items for this product
        warehouse_items = WarehouseInventory.query.filter_by(product_id=product_id).all()
        total_available = sum(item.quantity for item in warehouse_items)
        
        if total_available < quantity:
            return {
                "error": f"Insufficient warehouse stock. Available: {total_available}, Requested: {quantity}"
            }
            
        # Deduct from warehouse (FIFO - first in, first out)
        remaining = quantity
        transferred_from = []
        
        for wh_item in sorted(warehouse_items, key=lambda x: x.received_date or datetime.min):
            if remaining <= 0:
                break
            deduct = min(wh_item.quantity, remaining)
            wh_item.quantity -= deduct
            remaining -= deduct
            transferred_from.append({
                "batch": wh_item.batch_number,
                "deducted": deduct
            })
            
        # Add to main stock
        product.stock = (product.stock or 0) + quantity
        
        # Record transfer
        transfer = WarehouseTransfer(
            product_id=product_id,
            quantity=quantity,
            from_warehouse=True,
            notes=notes or 'AI Agent transfer'
        )
        self.db.session.add(transfer)
        self.db.session.commit()
        
        return {
            "success": True,
            "transfer_id": transfer.id,
            "product_id": product_id,
            "product_name": product.name,
            "quantity_transferred": quantity,
            "new_stock_level": product.stock,
            "transferred_from": transferred_from
        }
        
    def get_sales_trends(self, days: int = 30, product_id: int = None, top_n: int = 10) -> Dict[str, Any]:
        """Get sales trend analysis"""
        Sale = self._get_model('Sale')
        SaleItem = self._get_model('SaleItem')
        Product = self._get_model('Product')
        
        from_date = datetime.utcnow() - timedelta(days=days)
        
        # Query sales in date range
        sales_query = Sale.query.filter(Sale.date >= from_date)
        sales = sales_query.all()
        
        # Aggregate sales by product
        product_sales = {}
        
        for sale in sales:
            for item in sale.items:
                if product_id and item.product_id != product_id:
                    continue
                    
                if item.product_id not in product_sales:
                    product_sales[item.product_id] = {
                        "product_id": item.product_id,
                        "product_name": item.product.name if item.product else "Unknown",
                        "total_quantity": 0,
                        "total_revenue": 0,
                        "sale_count": 0
                    }
                    
                product_sales[item.product_id]["total_quantity"] += item.quantity
                product_sales[item.product_id]["total_revenue"] += (item.price * item.quantity)
                product_sales[item.product_id]["sale_count"] += 1
                
        # Sort by quantity sold and get top N
        sorted_sales = sorted(product_sales.values(), key=lambda x: x["total_quantity"], reverse=True)
        top_sales = sorted_sales[:top_n]
        
        return {
            "period_days": days,
            "total_products_sold": len(product_sales),
            "top_selling_products": top_sales
        }
        
    def get_product_details(self, product_id: int = None, barcode: str = None) -> Dict[str, Any]:
        """Get detailed product information"""
        Product = self._get_model('Product')
        
        if product_id:
            product = Product.query.get(product_id)
        elif barcode:
            product = Product.query.filter_by(barcode=barcode).first()
        else:
            return {"error": "Either product_id or barcode must be provided"}
            
        if not product:
            return {"error": "Product not found"}
            
        # Get supplier price agreements
        supplier_prices = []
        for sp in product.supplier_prices:
            supplier_prices.append({
                "supplier_id": sp.supplier_id,
                "supplier_name": sp.supplier.name if sp.supplier else None,
                "agreed_price": float(sp.agreed_price),
                "valid_to": sp.valid_to.isoformat() if sp.valid_to else None
            })
            
        return {
            "product_id": product.id,
            "name": product.name,
            "barcode": product.barcode,
            "category": product.category,
            "price": float(product.price or 0),
            "cost": float(product.cost or 0),
            "stock": product.stock or 0,
            "reorder_point": product.reorder_point or 0,
            "reorder_quantity": product.reorder_quantity or 0,
            "reorder_enabled": product.reorder_enabled,
            "tax_rate": float(product.tax_rate or 0),
            "supplier_prices": supplier_prices
        }
        
    def suggest_reorder_quantities(self) -> Dict[str, Any]:
        """Analyze and suggest optimal reorder quantities based on sales trends"""
        Product = self._get_model('Product')
        SaleItem = self._get_model('SaleItem')
        Sale = self._get_model('Sale')
        
        # Get low stock items
        low_stock = self.get_low_stock_items()
        
        suggestions = []
        
        for item in low_stock.get('items', []):
            product_id = item['product_id']
            
            # Get 30-day sales velocity
            from_date = datetime.utcnow() - timedelta(days=30)
            sales = SaleItem.query.join(Sale).filter(
                SaleItem.product_id == product_id,
                Sale.date >= from_date
            ).all()
            
            total_sold = sum(s.quantity for s in sales)
            daily_velocity = total_sold / 30 if total_sold > 0 else 0.1  # Minimum 0.1 per day
            
            # Calculate suggested quantity based on velocity
            # Suggest 30 days of stock plus reorder point buffer
            product = Product.query.get(product_id)
            reorder_point = product.reorder_point or 10
            
            suggested_qty = max(int(daily_velocity * 45), reorder_point * 2)
            
            # Round to nearest 10 for practicality
            suggested_qty = ((suggested_qty + 9) // 10) * 10
            
            suggestions.append({
                "product_id": product_id,
                "name": item['name'],
                "current_stock": item['current_stock'],
                "daily_sales_velocity": round(daily_velocity, 2),
                "suggested_reorder_qty": suggested_qty,
                "unit_cost": item['unit_cost'],
                "estimated_cost": round(suggested_qty * item['unit_cost'], 2)
            })
            
        return {
            "analysis_period_days": 30,
            "suggestions": suggestions,
            "total_estimated_cost": round(sum(s['estimated_cost'] for s in suggestions), 2)
        }
        
    def get_supplier_price_for_product(self, product_id: int, supplier_id: int) -> Dict[str, Any]:
        """Get supplier price agreement for a product"""
        SupplierPriceAgreement = self._get_model('SupplierPriceAgreement')
        
        agreement = SupplierPriceAgreement.query.filter_by(
            product_id=product_id,
            supplier_id=supplier_id
        ).first()
        
        if not agreement:
            return {
                "has_agreement": False,
                "message": "No price agreement found for this product and supplier combination."
            }
            
        return {
            "has_agreement": True,
            "product_id": product_id,
            "supplier_id": supplier_id,
            "agreed_price": float(agreement.agreed_price),
            "valid_from": agreement.valid_from.isoformat() if agreement.valid_from else None,
            "valid_to": agreement.valid_to.isoformat() if agreement.valid_to else None
        }
        
    def _generate_random_suffix(self) -> str:
        """Generate a random suffix for PO numbers"""
        import uuid
        return uuid.uuid4().hex[:6].upper()


def get_all_tools() -> Dict[str, Dict]:
    """Get all tool schemas"""
    return TOOL_SCHEMAS


def create_tools_instance(db, models: Dict) -> AITools:
    """Factory function to create an AITools instance"""
    return AITools(db, models)
