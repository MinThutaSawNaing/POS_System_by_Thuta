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
    "search_products": {
        "name": "search_products",
        "description": "Search products in the active branch by name. Returns stock level, price, cost and status for every matching product.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Product name or part of a name to search for."
                }
            },
            "required": ["query"]
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
    },
    "get_current_branch_context": {
        "name": "get_current_branch_context",
        "description": "Get the active POS branch that scopes all assistant results.",
        "parameters": {"type": "object", "properties": {}}
    },
    "get_category_summary": {
        "name": "get_category_summary",
        "description": "List categories and their active-branch product and supplier counts.",
        "parameters": {"type": "object", "properties": {"active_only": {"type": "boolean", "description": "Only active categories when true."}}}
    },
    "get_promotion_summary": {
        "name": "get_promotion_summary",
        "description": "List active-branch promotions, optionally filtered to active, upcoming, or expired status.",
        "parameters": {"type": "object", "properties": {"status": {"type": "string", "description": "Optional: active, upcoming, expired, or all."}, "limit": {"type": "integer", "description": "Maximum results, 1 to 100."}}}
    },
    "get_customer_summary": {
        "name": "get_customer_summary",
        "description": "Find active-branch customers and their outstanding debt balances.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Optional customer name, phone, or email search."}, "limit": {"type": "integer", "description": "Maximum results, 1 to 100."}}}
    },
    "get_debt_summary": {
        "name": "get_debt_summary",
        "description": "Summarize active-branch customer debts, overdue balances, and aging status.",
        "parameters": {"type": "object", "properties": {"status": {"type": "string", "description": "Optional: pending, partial, overdue, paid, or all."}, "limit": {"type": "integer", "description": "Maximum debt records, 1 to 100."}}}
    },
    "get_delivery_summary": {
        "name": "get_delivery_summary",
        "description": "Summarize active-branch deliveries by stage and show open delivery work.",
        "parameters": {"type": "object", "properties": {"stage": {"type": "string", "description": "Optional delivery stage filter."}, "priority": {"type": "string", "description": "Optional priority filter."}, "limit": {"type": "integer", "description": "Maximum results, 1 to 100."}}}
    },
    "get_return_exchange_summary": {
        "name": "get_return_exchange_summary",
        "description": "List recent active-branch returns and exchanges with refund and collection totals.",
        "parameters": {"type": "object", "properties": {"mode": {"type": "string", "description": "Optional: return or exchange."}, "limit": {"type": "integer", "description": "Maximum results, 1 to 100."}}}
    },
    "get_warehouse_transfer_history": {
        "name": "get_warehouse_transfer_history",
        "description": "List recent active-branch warehouse-to-stock transfers.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Maximum results, 1 to 100."}}}
    },
    "get_sales_summary": {
        "name": "get_sales_summary",
        "description": "Summarize active-branch sales totals, transaction count, payment methods, and recent sales for a period.",
        "parameters": {"type": "object", "properties": {"days": {"type": "integer", "description": "Days to summarize, 1 to 365. Default 30."}, "limit": {"type": "integer", "description": "Maximum recent sales, 1 to 100."}}}
    }
}


class AITools:
    """Container for all AI tool functions with database access"""
    
    def __init__(self, db, models):
        self.db = db
        self.models = models
        self.context = {}

    def set_context(self, context: Optional[Dict[str, Any]] = None):
        """Set trusted request context; model-controlled tool arguments never choose branch scope."""
        self.context = dict(context or {})

    def _branch_id(self):
        return self.context.get('branch_id')

    def _branch_filter(self, query, model):
        branch_id = self._branch_id()
        return query.filter(model.branch_id == branch_id) if branch_id is not None else query

    def _limit(self, limit, default=20):
        try:
            return max(1, min(int(limit or default), 100))
        except (TypeError, ValueError):
            return default

    def _scope(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload['branch_id'] = self._branch_id()
        return payload
        
    def _get_model(self, name):
        """Get a model class by name"""
        return self.models.get(name)
        
    def get_inventory_status(self, product_id: int = None, category: str = None, 
                            low_stock_only: bool = False) -> Dict[str, Any]:
        """Get current inventory status"""
        Product = self._get_model('Product')
        query = self._branch_filter(Product.query, Product)
        
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
            
        return self._scope({
            "total_products": len(result),
            "inventory": result
        })
        
    def get_low_stock_items(self) -> Dict[str, Any]:
        """Get all low stock items with suggested reorder quantities"""
        Product = self._get_model('Product')
        products = self._branch_filter(Product.query.filter_by(reorder_enabled=True), Product).all()
        
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
                
        return self._scope({
            "summary": {
                "low_stock_count": len(low_stock_items),
                "out_of_stock_count": out_of_stock_count
            },
            "items": low_stock_items
        })
        
    def search_products(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Search products in the active branch by name and return inventory-style rows."""
        Product = self._get_model('Product')
        if not Product or not (query or '').strip():
            return self._scope({"total_products": 0, "inventory": []})
        pattern = f"%{query.strip()}%"
        products = self._branch_filter(
            Product.query.filter(Product.name.ilike(pattern)), Product
        ).limit(self._limit(limit)).all()
        result = []
        for product in products:
            current_stock = int(product.stock or 0)
            reorder_point = max(int(product.reorder_point or 0), 0)
            is_low = bool(product.reorder_enabled) and current_stock <= reorder_point
            result.append({
                "product_id": product.id,
                "name": product.name,
                "barcode": product.barcode,
                "category": product.category,
                "current_stock": current_stock,
                "reorder_point": reorder_point,
                "reorder_enabled": bool(product.reorder_enabled),
                "status": "out_of_stock" if current_stock <= 0 else ("low_stock" if is_low else "ok"),
                "price": float(product.price or 0),
                "cost": float(product.cost or 0)
            })
        return self._scope({"total_products": len(result), "inventory": result})
        
    def get_supplier_list(self, active_only: bool = True, category: str = None) -> Dict[str, Any]:
        """Get list of suppliers"""
        Supplier = self._get_model('Supplier')
        query = self._branch_filter(Supplier.query, Supplier)
        
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
        
        supplier = self._branch_filter(Supplier.query.filter_by(id=supplier_id), Supplier).first()
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
        query = self._branch_filter(PurchaseOrder.query, PurchaseOrder)
        
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
        query = self._branch_filter(WarehouseInventory.query, WarehouseInventory)
        
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
        sales_query = self._branch_filter(Sale.query.filter(Sale.date >= from_date), Sale)
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
            product = self._branch_filter(Product.query.filter_by(id=product_id), Product).first()
        elif barcode:
            product = self._branch_filter(Product.query.filter_by(barcode=barcode), Product).first()
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
                Sale.date >= from_date,
                Sale.branch_id == self._branch_id()
            ).all()
            
            total_sold = sum(s.quantity for s in sales)
            daily_velocity = total_sold / 30 if total_sold > 0 else 0.1  # Minimum 0.1 per day
            
            # Calculate suggested quantity based on velocity
            # Suggest 30 days of stock plus reorder point buffer
            product = self._branch_filter(Product.query.filter_by(id=product_id), Product).first()
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
        
        Product = self._get_model('Product')
        Supplier = self._get_model('Supplier')
        product = self._branch_filter(Product.query.filter_by(id=product_id), Product).first()
        supplier = self._branch_filter(Supplier.query.filter_by(id=supplier_id), Supplier).first()
        if not product or not supplier:
            return {"has_agreement": False, "message": "Product or supplier was not found in the active branch."}
        agreement = SupplierPriceAgreement.query.filter_by(product_id=product_id, supplier_id=supplier_id).first()
        
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

    def get_current_branch_context(self) -> Dict[str, Any]:
        """Return the trusted branch scope applied to every assistant query."""
        Branch = self._get_model('Branch')
        branch_id = self._branch_id()
        branch = Branch.query.filter_by(id=branch_id, is_active=True).first() if Branch and branch_id else None
        if not branch:
            return {"branch_id": branch_id, "message": "No active branch is available."}
        return {"branch_id": branch.id, "name": branch.name, "code": branch.code, "is_default": bool(branch.is_default)}

    def get_category_summary(self, active_only: bool = True) -> Dict[str, Any]:
        Category, Product, Supplier = self._get_model('Category'), self._get_model('Product'), self._get_model('Supplier')
        query = self._branch_filter(Category.query, Category)
        if active_only:
            query = query.filter_by(is_active=True)
        categories = query.order_by(Category.sort_order.asc(), Category.name.asc()).all()
        rows = []
        for category in categories:
            rows.append({"id": category.id, "name": category.name, "description": category.description,
                         "is_active": bool(category.is_active), "product_count": self._branch_filter(Product.query.filter_by(category_id=category.id), Product).count(),
                         "supplier_count": self._branch_filter(Supplier.query.filter_by(category_id=category.id), Supplier).count()})
        return self._scope({"total_categories": len(rows), "categories": rows})

    def get_promotion_summary(self, status: str = "all", limit: int = 20) -> Dict[str, Any]:
        Promotion, Product = self._get_model('Promotion'), self._get_model('Product')
        now = datetime.utcnow()
        promotions = Promotion.query.join(Product).filter(Product.branch_id == self._branch_id()).order_by(Promotion.end_date.asc()).limit(self._limit(limit)).all()
        rows = []
        for promo in promotions:
            state = 'active' if promo.start_date <= now <= promo.end_date else ('upcoming' if promo.start_date > now else 'expired')
            if status and status.lower() not in ('all', state):
                continue
            rows.append({"id": promo.id, "product_id": promo.product_id, "product_name": promo.product.name if promo.product else None,
                         "discount_type": promo.discount_type, "discount_value": float(promo.discount_value or 0), "status": state,
                         "start_date": promo.start_date.isoformat() if promo.start_date else None, "end_date": promo.end_date.isoformat() if promo.end_date else None})
        return self._scope({"total_promotions": len(rows), "promotions": rows})

    def get_customer_summary(self, query: str = None, limit: int = 20) -> Dict[str, Any]:
        Customer, Debt = self._get_model('Customer'), self._get_model('Debt')
        customers = self._branch_filter(Customer.query, Customer)
        if query:
            pattern = f"%{query.strip()}%"
            customers = customers.filter((Customer.name.ilike(pattern)) | (Customer.phone.ilike(pattern)) | (Customer.email.ilike(pattern)))
        rows = []
        for customer in customers.order_by(Customer.name.asc()).limit(self._limit(limit)).all():
            balance = sum(float(debt.balance or 0) for debt in Debt.query.filter_by(customer_id=customer.id, branch_id=self._branch_id()).all())
            rows.append({"id": customer.id, "name": customer.name, "phone": customer.phone, "email": customer.email, "outstanding_balance": round(balance, 2)})
        return self._scope({"total_customers": len(rows), "customers": rows})

    def get_debt_summary(self, status: str = "all", limit: int = 20) -> Dict[str, Any]:
        Debt = self._get_model('Debt')
        debts = self._branch_filter(Debt.query, Debt).order_by(Debt.due_date.asc()).all()
        rows, totals = [], {"pending": 0.0, "partial": 0.0, "overdue": 0.0, "paid": 0.0}
        now = datetime.utcnow()
        for debt in debts:
            state = 'paid' if float(debt.balance or 0) <= 0 else ('overdue' if debt.due_date and debt.due_date < now else ('partial' if float(debt.balance or 0) < float(debt.amount or 0) else 'pending'))
            totals[state] += float(debt.balance or 0)
            if status and status.lower() not in ('all', state):
                continue
            if len(rows) < self._limit(limit):
                rows.append({"id": debt.id, "customer_name": debt.customer.name if debt.customer else 'Unknown', "balance": float(debt.balance or 0), "amount": float(debt.amount or 0), "status": state, "due_date": debt.due_date.isoformat() if debt.due_date else None})
        return self._scope({"totals_by_status": {key: round(value, 2) for key, value in totals.items()}, "debts": rows})

    def get_delivery_summary(self, stage: str = None, priority: str = None, limit: int = 20) -> Dict[str, Any]:
        Delivery = self._get_model('Delivery')
        deliveries = self._branch_filter(Delivery.query, Delivery)
        if stage: deliveries = deliveries.filter_by(stage=stage)
        if priority: deliveries = deliveries.filter_by(priority=priority)
        items = deliveries.order_by(Delivery.created_at.desc()).limit(self._limit(limit)).all()
        stages = {}
        for delivery in self._branch_filter(Delivery.query, Delivery).all(): stages[delivery.stage] = stages.get(delivery.stage, 0) + 1
        rows = [{"id": d.id, "delivery_number": d.delivery_number, "customer_name": d.customer.name if d.customer else None, "stage": d.stage, "priority": d.priority, "scheduled_at": d.scheduled_at.isoformat() if d.scheduled_at else None} for d in items]
        return self._scope({"stage_counts": stages, "open_high_priority": sum(1 for d in items if d.priority in ('high', 'urgent') and d.stage not in ('delivered', 'cancelled')), "deliveries": rows})

    def get_return_exchange_summary(self, mode: str = None, limit: int = 20) -> Dict[str, Any]:
        ReturnExchange, Sale = self._get_model('ReturnExchange'), self._get_model('Sale')
        query = ReturnExchange.query.join(Sale, ReturnExchange.original_sale_id == Sale.id).filter(Sale.branch_id == self._branch_id())
        if mode: query = query.filter(ReturnExchange.mode == mode)
        workflows = query.order_by(ReturnExchange.created_at.desc()).limit(self._limit(limit)).all()
        rows = [{"workflow_id": w.workflow_id, "mode": w.mode, "return_total": float(w.return_total or 0), "exchange_total": float(w.exchange_total or 0), "refund_amount": float(w.refund_amount or 0), "collected_amount": float(w.collected_amount or 0), "created_at": w.created_at.isoformat() if w.created_at else None} for w in workflows]
        return self._scope({"total_workflows": len(rows), "workflows": rows})

    def get_warehouse_transfer_history(self, limit: int = 20) -> Dict[str, Any]:
        Transfer = self._get_model('WarehouseTransfer')
        transfers = self._branch_filter(Transfer.query, Transfer).order_by(Transfer.created_at.desc()).limit(self._limit(limit)).all()
        rows = [{"id": t.id, "product_name": t.product.name if t.product else 'Unknown', "quantity": t.quantity, "batch_number": t.batch_number, "performed_by": t.performer.username if t.performer else None, "created_at": t.created_at.isoformat() if t.created_at else None} for t in transfers]
        return self._scope({"total_transfers": len(rows), "transfers": rows})

    def get_sales_summary(self, days: int = 30, limit: int = 10) -> Dict[str, Any]:
        Sale = self._get_model('Sale')
        try: days = max(1, min(int(days), 365))
        except (TypeError, ValueError): days = 30
        since = datetime.utcnow() - timedelta(days=days)
        sales = self._branch_filter(Sale.query.filter(Sale.date >= since), Sale).order_by(Sale.date.desc()).all()
        methods = {}
        for sale in sales: methods[sale.payment_method or 'unknown'] = methods.get(sale.payment_method or 'unknown', 0) + float(sale.total or 0)
        recent = [{"transaction_id": s.transaction_id, "total": float(s.total or 0), "payment_method": s.payment_method, "date": s.date.isoformat() if s.date else None} for s in sales[:self._limit(limit, 10)]]
        return self._scope({"period_days": days, "transaction_count": len(sales), "total_sales": round(sum(float(s.total or 0) for s in sales), 2), "payment_method_totals": {key: round(value, 2) for key, value in methods.items()}, "recent_sales": recent})
        
    def _generate_random_suffix(self) -> str:
        """Generate a random suffix for PO numbers"""
        import uuid
        return uuid.uuid4().hex[:6].upper()


def get_all_tools(read_only: bool = False) -> Dict[str, Dict]:
    """Get tool schemas; agent callers receive only safe read-only tools."""
    if not read_only:
        return TOOL_SCHEMAS
    mutation_tools = {
        'create_purchase_order', 'approve_purchase_order',
        'cancel_purchase_order', 'create_warehouse_transfer'
    }
    return {name: schema for name, schema in TOOL_SCHEMAS.items() if name not in mutation_tools}


def create_tools_instance(db, models: Dict) -> AITools:
    """Factory function to create an AITools instance"""
    return AITools(db, models)
