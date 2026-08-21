"""
AI Tools Module for POS System
Defines all tools the AI Agent can use for inventory and procurement tasks

================================================================================
EXTENSIBILITY PATTERN ("trainable" backbone)
================================================================================
Every tool is registered exactly once in TOOL_METADATA. Adding a future
feature requires exactly TWO changes:

    1. Add ONE entry to TOOL_METADATA below (name, description, parameters,
       category, mutates, requires_role, description_one_line,
       result_size_hint).
    2. Add ONE method with the same name on the AITools class.

TOOL_SCHEMAS (the legacy format consumed by agent callers/tests) is DERIVED
from TOOL_METADATA, so old imports keep working unchanged. Read-only callers
get write tools filtered out automatically via the ``mutates`` flag
(see ``get_all_tools``).

Safety rails every WRITE tool (mutates=True) must follow:
    - Validate ALL inputs (types, ranges, referenced ids exist within the
      current branch scope via self._branch_filter / self._branch_id()).
    - Return {"success": True, ...} or {"error": "..."} dicts.
    - Include audit-trail info: entity id + changed fields.
    - Serialize money as plain 2-decimal STRINGS (e.g. "12.34") using
      money_plain(); never return raw floats for money in NEW tools.
"""

import json
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from decimal import Decimal, ROUND_HALF_UP

MONEY_QUANT = Decimal('0.01')

# Delivery stages mirror app.py's DELIVERY_STAGE_FLOW keys (app.py cannot be
# imported here without the Flask app context, so the valid set is mirrored).
DELIVERY_STAGES = {'to_deliver', 'packaged', 'delivering', 'delivered', 'cancelled'}
DELIVERY_STAGE_FLOW = {
    'to_deliver': ['packaged', 'cancelled'],
    'packaged': ['delivering', 'cancelled'],
    'delivering': ['delivered', 'cancelled'],
    'delivered': [],
    'cancelled': []
}

def money_dec(value):
    """Convert a value to a finite Decimal, defaulting to 0 for None/NaN/inf/unparseable input."""
    if value is None:
        return Decimal('0')
    try:
        result = Decimal(str(value).replace(',', ''))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal('0')
    if not result.is_finite():
        return Decimal('0')
    return result

def money_str(value):
    """Format a money value as a quantized 2-decimal string (e.g. '1,234.50')."""
    return f"{money_dec(value).quantize(MONEY_QUANT):,.2f}"

def money_plain(value):
    """Format a money value as a plain 2-decimal string WITHOUT thousands separators (e.g. '1234.50').

    Used by NEW tools so downstream consumers can parse amounts numerically."""
    return f"{money_dec(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)}"

# Tool schema definitions for the AI (parameter schemas only; enriched into
# TOOL_METADATA right below -- do not add metadata fields here).
_BASE_TOOL_PARAMETER_SCHEMAS: Dict[str, Dict] = {
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
    },
    "upsert_product": {
        "name": "upsert_product",
        "description": "Create a product or update an existing product's price, cost, tax rate, stock, category, and reorder settings. Requires manager role.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Product name (required)."},
                "price": {"type": "number", "description": "Selling price, >= 0 (required)."},
                "cost": {"type": "number", "description": "Unit cost, >= 0. Optional."},
                "tax_rate": {"type": "number", "description": "Tax rate percent between 0 and 100. Default 0."},
                "stock": {"type": "integer", "description": "Initial/updated stock level, >= 0. Default 0."},
                "category": {"type": "string", "description": "Optional category name."},
                "reorder_point": {"type": "integer", "description": "Reorder point, >= 0. Default 10."},
                "reorder_quantity": {"type": "integer", "description": "Reorder quantity, >= 0. Default 50."},
                "barcode": {"type": "string", "description": "Optional barcode."},
                "product_id": {"type": "integer", "description": "Provide to update an existing product instead of creating one."}
            },
            "required": ["name", "price"]
        }
    },
    "adjust_product_stock": {
        "name": "adjust_product_stock",
        "description": "Apply a signed stock delta (positive or negative) to a product with a mandatory reason. Resulting stock must stay >= 0. Requires manager role.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "description": "The product to adjust."},
                "delta": {"type": "integer", "description": "Signed quantity change; must not be zero."},
                "reason": {"type": "string", "description": "Mandatory audit reason for the adjustment."}
            },
            "required": ["product_id", "delta", "reason"]
        }
    },
    "create_promotion": {
        "name": "create_promotion",
        "description": "Create a percent or fixed-amount discount promotion for a product over a date range. Requires manager role.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "description": "The product to promote."},
                "discount_type": {"type": "string", "description": "'percent' or 'fixed'."},
                "discount_value": {"type": "number", "description": "Percent (0 < v <= 100) for 'percent', positive amount for 'fixed'."},
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD, must be on or after start_date."}
            },
            "required": ["product_id", "discount_type", "discount_value", "start_date", "end_date"]
        }
    },
    "register_customer": {
        "name": "register_customer",
        "description": "Register a new customer in the active branch with optional phone, email, and address.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer name (required)."},
                "phone": {"type": "string", "description": "Optional phone number."},
                "email": {"type": "string", "description": "Optional email address."},
                "address": {"type": "string", "description": "Optional address."}
            },
            "required": ["name"]
        }
    },
    "record_debt_payment": {
        "name": "record_debt_payment",
        "description": "Record a payment against a customer debt. Uses exact decimal math; rejects amounts <= 0 or exceeding balance; marks debt paid at zero balance.",
        "parameters": {
            "type": "object",
            "properties": {
                "debt_id": {"type": "integer", "description": "The debt record to pay."},
                "amount": {"type": "number", "description": "Payment amount, > 0 and <= remaining balance."},
                "notes": {"type": "string", "description": "Optional payment notes."}
            },
            "required": ["debt_id", "amount"]
        }
    },
    "update_delivery_stage": {
        "name": "update_delivery_stage",
        "description": "Move a delivery to its next valid stage: to_deliver -> packaged -> delivering -> delivered, with cancelled allowed until delivered.",
        "parameters": {
            "type": "object",
            "properties": {
                "delivery_id": {"type": "integer", "description": "The delivery to update."},
                "stage": {"type": "string", "description": "One of: to_deliver, packaged, delivering, delivered, cancelled."}
            },
            "required": ["delivery_id", "stage"]
        }
    }
}

# ==============================================================================
# TOOL_METADATA -- the single source of truth for every tool.
# Each entry: name, description, parameters, category
# (inventory|sales|purchasing|customers|debts|deliveries|promotions|system),
# mutates (bool), requires_role (None|'manager'|'boss'),
# description_one_line (token-cheap planning prompts), result_size_hint
# ('small'|'medium'|'large').
# Adding a tool = ONE entry here + ONE method on AITools. See module docstring.
# ==============================================================================
_TOOL_META = {
    # --- read tools ---
    "get_inventory_status":            ("inventory",   False, None,     "Current stock levels and reorder status for all or one product.", "medium"),
    "get_low_stock_items":             ("inventory",   False, None,     "Products at or below reorder point with suggested reorder quantities.", "medium"),
    "search_products":                 ("inventory",   False, None,     "Search active-branch products by name; returns stock, price, cost.", "small"),
    "get_supplier_list":               ("purchasing",  False, None,     "List suppliers with contact info, ratings, and performance.", "medium"),
    "get_supplier_details":            ("purchasing",  False, None,     "Detailed supplier info including price agreements and recent POs.", "large"),
    "get_purchase_orders":             ("purchasing",  False, None,     "List purchase orders filtered by status, supplier, or date.", "medium"),
    "get_warehouse_inventory":         ("inventory",   False, None,     "Warehouse stock received but not yet transferred to main stock.", "medium"),
    "get_sales_trends":                ("sales",       False, None,     "Sales trend analysis per product over a period for reorder decisions.", "large"),
    "get_product_details":             ("inventory",   False, None,     "Full product detail: stock, pricing, tax, supplier agreements.", "small"),
    "suggest_reorder_quantities":      ("inventory",   False, None,     "Suggested reorder quantities from 30-day sales velocity analysis.", "medium"),
    "get_supplier_price_for_product":  ("purchasing",  False, None,     "Agreed supplier price for a product, if an agreement exists.", "small"),
    "get_current_branch_context":      ("system",      False, None,     "Active POS branch that scopes all assistant results.", "small"),
    "get_category_summary":            ("inventory",   False, None,     "Categories with active-branch product and supplier counts.", "small"),
    "get_promotion_summary":           ("promotions",  False, None,     "Active-branch promotions filtered by active/upcoming/expired.", "medium"),
    "get_customer_summary":            ("customers",   False, None,     "Find active-branch customers and outstanding debt balances.", "medium"),
    "get_debt_summary":                ("debts",       False, None,     "Summarize customer debts, overdue balances, and aging status.", "medium"),
    "get_delivery_summary":            ("deliveries",  False, None,     "Deliveries by stage plus open delivery work for the branch.", "medium"),
    "get_return_exchange_summary":     ("sales",       False, None,     "Recent returns/exchanges with refund and collection totals.", "medium"),
    "get_warehouse_transfer_history":  ("inventory",   False, None,     "Recent warehouse-to-stock transfers.", "small"),
    "get_sales_summary":               ("sales",       False, None,     "Sales totals, transaction count, payment methods for a period.", "medium"),
    # --- write tools ---
    "create_purchase_order":           ("purchasing",  True,  'manager', "Create a draft purchase order for one or more products from a supplier.", "small"),
    "approve_purchase_order":          ("purchasing",  True,  'manager', "Approve a pending purchase order (pending -> approved).", "small"),
    "cancel_purchase_order":           ("purchasing",  True,  'manager', "Cancel a draft/pending/approved purchase order with a reason.", "small"),
    "create_warehouse_transfer":       ("inventory",   True,  'manager', "Move warehouse stock into main product stock (FIFO deduction).", "small"),
    "upsert_product":                  ("inventory",   True,  'manager', "Create a product or update an existing one's price/cost/stock/reorder settings.", "small"),
    "adjust_product_stock":            ("inventory",   True,  'manager', "Apply a signed stock delta to a product with a mandatory reason; result must stay >= 0.", "small"),
    "create_promotion":                ("promotions",  True,  'manager', "Create a percent or fixed discount promotion for a product over a date range.", "small"),
    "register_customer":               ("customers",   True,  'staff',   "Register a new customer in the active branch with optional contact details.", "small"),
    "record_debt_payment":             ("debts",       True,  'staff',   "Record a payment against a customer debt using exact decimal math; marks paid at zero balance.", "small"),
    "update_delivery_stage":           ("deliveries",  True,  'staff',   "Advance a delivery to its next valid stage (to_deliver -> packaged -> delivering -> delivered/cancelled).", "small"),
}

TOOL_METADATA: Dict[str, Dict[str, Any]] = {}
for _name, _schema in _BASE_TOOL_PARAMETER_SCHEMAS.items():
    _category, _mutates, _role, _one_line, _size = _TOOL_META[_name]
    TOOL_METADATA[_name] = {
        **_schema,
        "category": _category,
        "mutates": _mutates,
        "requires_role": _role,
        "description_one_line": _one_line,
        "result_size_hint": _size,
    }

# Backward-compatible legacy schema view, derived from the registry.
TOOL_SCHEMAS: Dict[str, Dict] = {
    name: {"name": meta["name"], "description": meta["description"], "parameters": meta["parameters"]}
    for name, meta in TOOL_METADATA.items()
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
                "price": money_str(product.price or 0),
                "cost": money_str(product.cost or 0)
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
                    "unit_cost": money_str(product.cost or 0),
                    "estimated_cost": money_str(suggested_qty * money_dec(product.cost or 0))
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
                "price": money_str(product.price or 0),
                "cost": money_str(product.cost or 0)
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
                "agreed_price": money_str(pa.agreed_price),
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
                "total_amount": money_str(po.total_amount or 0),
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
                    "unit_cost": money_str(item.unit_cost or 0)
                })

            result.append({
                "po_id": po.id,
                "po_number": po.po_number,
                "supplier_id": po.supplier_id,
                "supplier_name": po.supplier.name if po.supplier else None,
                "status": po.status,
                "total_amount": money_str(po.total_amount or 0),
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
        
        total_amount = Decimal('0')
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
            total_amount += money_dec(quantity) * money_dec(unit_cost)
            
            created_items.append({
                "product_id": product_id,
                "product_name": product.name,
                "quantity": quantity,
                "unit_cost": money_str(unit_cost)
            })
            
        po.total_amount = total_amount
        self.db.session.commit()
        
        return {
            "success": True,
            "po_id": po.id,
            "po_number": po_number,
            "supplier_name": supplier.name,
            "total_amount": money_str(total_amount),
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
                    "unit_cost": money_str(item.unit_cost or 0)
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
                "agreed_price": money_str(sp.agreed_price),
                "valid_to": sp.valid_to.isoformat() if sp.valid_to else None
            })
            
        return {
            "product_id": product.id,
            "name": product.name,
            "barcode": product.barcode,
            "category": product.category,
            "price": money_str(product.price or 0),
            "cost": money_str(product.cost or 0),
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
                "estimated_cost": money_str(suggested_qty * money_dec(item['unit_cost']))
            })
            
        return {
            "analysis_period_days": 30,
            "suggestions": suggestions,
            "total_estimated_cost": money_str(sum((money_dec(s['estimated_cost']) for s in suggestions), Decimal('0')))
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
            "agreed_price": money_str(agreement.agreed_price),
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
                         "discount_type": promo.discount_type, "discount_value": money_str(promo.discount_value or 0), "status": state,
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
            balance = sum((money_dec(debt.balance or 0) for debt in Debt.query.filter_by(customer_id=customer.id, branch_id=self._branch_id()).all()), Decimal('0'))
            rows.append({"id": customer.id, "name": customer.name, "phone": customer.phone, "email": customer.email, "outstanding_balance": money_str(balance)})
        return self._scope({"total_customers": len(rows), "customers": rows})

    def get_debt_summary(self, status: str = "all", limit: int = 20) -> Dict[str, Any]:
        Debt = self._get_model('Debt')
        debts = self._branch_filter(Debt.query, Debt).order_by(Debt.due_date.asc()).all()
        rows, totals = [], {"pending": Decimal('0'), "partial": Decimal('0'), "overdue": Decimal('0'), "paid": Decimal('0')}
        now = datetime.utcnow()
        for debt in debts:
            debt_balance = money_dec(debt.balance or 0)
            state = 'paid' if debt_balance <= 0 else ('overdue' if debt.due_date and debt.due_date < now else ('partial' if debt_balance < money_dec(debt.amount or 0) else 'pending'))
            totals[state] += debt_balance
            if status and status.lower() not in ('all', state):
                continue
            if len(rows) < self._limit(limit):
                rows.append({"id": debt.id, "customer_name": debt.customer.name if debt.customer else 'Unknown', "balance": money_str(debt_balance), "amount": money_str(debt.amount or 0), "status": state, "due_date": debt.due_date.isoformat() if debt.due_date else None})
        return self._scope({"totals_by_status": {key: money_str(value) for key, value in totals.items()}, "debts": rows})

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
        rows = [{"workflow_id": w.workflow_id, "mode": w.mode, "return_total": money_str(w.return_total or 0), "exchange_total": money_str(w.exchange_total or 0), "refund_amount": money_str(w.refund_amount or 0), "collected_amount": money_str(w.collected_amount or 0), "created_at": w.created_at.isoformat() if w.created_at else None} for w in workflows]
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
        for sale in sales: methods[sale.payment_method or 'unknown'] = methods.get(sale.payment_method or 'unknown', Decimal('0')) + money_dec(sale.total or 0)
        recent = [{"transaction_id": s.transaction_id, "total": money_str(s.total or 0), "payment_method": s.payment_method, "date": s.date.isoformat() if s.date else None} for s in sales[:self._limit(limit, 10)]]
        return self._scope({"period_days": days, "transaction_count": len(sales), "total_sales": money_str(sum((money_dec(s.total or 0) for s in sales), Decimal('0'))), "payment_method_totals": {key: money_str(value) for key, value in methods.items()}, "recent_sales": recent})
        
    # ==================================================================
    # WRITE TOOLS (mutates=True). Pattern: validate all inputs, scope
    # lookups to the active branch via self._branch_filter, return
    # {"success": True, ...} or {"error": ...} with audit info, and
    # serialize money as plain 2-decimal strings via money_plain().
    # ==================================================================

    def upsert_product(self, name: str, price, cost=None, tax_rate=0.0, stock: int = 0,
                       category: str = None, reorder_point: int = 10,
                       reorder_quantity: int = 50, barcode: str = None,
                       product_id: int = None) -> Dict[str, Any]:
        """Create a product or update an existing one (manager only)."""
        Product = self._get_model('Product')
        if not isinstance(name, str) or not name.strip():
            return {"error": "Product name is required"}
        name = name.strip()
        price_dec = money_dec(price)
        if price_dec < 0:
            return {"error": "Price must be >= 0"}
        cost_dec = money_dec(cost) if cost is not None else None
        if cost_dec is not None and cost_dec < 0:
            return {"error": "Cost must be >= 0"}
        try:
            tax_rate_val = float(tax_rate)
        except (TypeError, ValueError):
            return {"error": "tax_rate must be a number between 0 and 100"}
        if not (0 <= tax_rate_val <= 100):
            return {"error": "tax_rate must be between 0 and 100"}
        try:
            stock_val = int(stock)
            reorder_point_val = int(reorder_point)
            reorder_quantity_val = int(reorder_quantity)
        except (TypeError, ValueError):
            return {"error": "stock, reorder_point and reorder_quantity must be integers"}
        if stock_val < 0 or reorder_point_val < 0 or reorder_quantity_val < 0:
            return {"error": "stock, reorder_point and reorder_quantity must be >= 0"}

        changed_fields = ["name", "price", "cost", "tax_rate", "stock", "category",
                          "reorder_point", "reorder_quantity", "barcode"]
        if product_id is not None:
            product = self._branch_filter(Product.query.filter_by(id=product_id), Product).first()
            if not product:
                return {"error": f"Product with ID {product_id} not found in the active branch"}
            product.name = name
            product.price = float(price_dec)
            product.cost = float(cost_dec) if cost_dec is not None else product.cost
            product.tax_rate = tax_rate_val
            product.stock = stock_val
            product.category = category if category is not None else product.category
            product.reorder_point = reorder_point_val
            product.reorder_quantity = reorder_quantity_val
            product.barcode = barcode if barcode is not None else product.barcode
            created = False
        else:
            duplicate = self._branch_filter(Product.query.filter_by(name=name), Product).first()
            if duplicate:
                return {"error": f"A product named '{name}' already exists in the active branch (ID {duplicate.id})"}
            product = Product(
                name=name,
                price=float(price_dec),
                cost=float(cost_dec) if cost_dec is not None else None,
                tax_rate=tax_rate_val,
                stock=stock_val,
                category=category,
                reorder_point=reorder_point_val,
                reorder_quantity=reorder_quantity_val,
                barcode=barcode,
                branch_id=self._branch_id()
            )
            self.db.session.add(product)
            created = True
        self.db.session.commit()
        return {
            "success": True,
            "created": created,
            "product_id": product.id,
            "product_name": product.name,
            "changed_fields": changed_fields,
            "price": money_plain(product.price),
            "cost": money_plain(product.cost or 0),
            "stock": int(product.stock or 0),
            "reorder_point": int(product.reorder_point or 0),
            "reorder_quantity": int(product.reorder_quantity or 0)
        }

    def adjust_product_stock(self, product_id: int, delta: int, reason: str) -> Dict[str, Any]:
        """Apply a signed stock delta with a mandatory audit reason (manager only)."""
        Product = self._get_model('Product')
        try:
            delta_val = int(delta)
        except (TypeError, ValueError):
            return {"error": "delta must be a non-zero integer"}
        if delta_val == 0:
            return {"error": "delta must be a non-zero integer"}
        if not isinstance(reason, str) or not reason.strip():
            return {"error": "A reason for the adjustment is required"}
        product = self._branch_filter(Product.query.filter_by(id=product_id), Product).first()
        if not product:
            return {"error": f"Product with ID {product_id} not found in the active branch"}
        old_stock = int(product.stock or 0)
        new_stock = old_stock + delta_val
        if new_stock < 0:
            return {"error": f"Adjustment would result in negative stock ({new_stock}). Current stock: {old_stock}, delta: {delta_val}"}
        product.stock = new_stock
        self.db.session.commit()
        return {
            "success": True,
            "product_id": product.id,
            "product_name": product.name,
            "previous_stock": old_stock,
            "delta": delta_val,
            "new_stock": new_stock,
            "reason": reason.strip()
        }

    def create_promotion(self, product_id: int, discount_type: str, discount_value,
                         start_date: str, end_date: str) -> Dict[str, Any]:
        """Create a percent or fixed discount promotion (manager only)."""
        Promotion = self._get_model('Promotion')
        Product = self._get_model('Product')
        if discount_type not in ('percent', 'fixed'):
            return {"error": "discount_type must be 'percent' or 'fixed'"}
        value_dec = money_dec(discount_value)
        if value_dec <= 0:
            return {"error": "discount_value must be greater than 0"}
        if discount_type == 'percent' and value_dec > 100:
            return {"error": "percent discount_value must be between 0 and 100"}
        try:
            start_dt = datetime.strptime(str(start_date).strip(), '%Y-%m-%d')
            end_dt = datetime.strptime(str(end_date).strip(), '%Y-%m-%d')
        except (TypeError, ValueError):
            return {"error": "start_date and end_date must be in YYYY-MM-DD format"}
        if end_dt < start_dt:
            return {"error": "end_date must be on or after start_date"}
        product = self._branch_filter(Product.query.filter_by(id=product_id), Product).first()
        if not product:
            return {"error": f"Product with ID {product_id} not found in the active branch"}
        promotion = Promotion(
            product_id=product.id,
            discount_type=discount_type,
            discount_value=float(value_dec),
            start_date=start_dt,
            end_date=end_dt
        )
        self.db.session.add(promotion)
        self.db.session.commit()
        return {
            "success": True,
            "promotion_id": promotion.id,
            "product_id": product.id,
            "product_name": product.name,
            "discount_type": discount_type,
            "discount_value": money_plain(value_dec),
            "start_date": start_dt.date().isoformat(),
            "end_date": end_dt.date().isoformat()
        }

    def register_customer(self, name: str, phone: str = None, email: str = None,
                          address: str = None) -> Dict[str, Any]:
        """Register a new customer in the active branch."""
        Customer = self._get_model('Customer')
        if not isinstance(name, str) or not name.strip():
            return {"error": "Customer name is required"}
        name = name.strip()
        if email is not None:
            email = str(email).strip() or None
            if email and '@' not in email:
                return {"error": "email must be a valid email address"}
        customer = Customer(
            name=name,
            phone=str(phone).strip() if phone else None,
            email=email,
            address=str(address).strip() if address else None,
            branch_id=self._branch_id()
        )
        self.db.session.add(customer)
        self.db.session.commit()
        return {
            "success": True,
            "customer_id": customer.id,
            "customer_name": customer.name,
            "phone": customer.phone,
            "email": customer.email,
            "branch_id": self._branch_id()
        }

    def record_debt_payment(self, debt_id: int, amount, notes: str = None) -> Dict[str, Any]:
        """Record a payment against a debt using Decimal-safe math (any role).

        Mirrors app.py's /api/debts/<id>/payment route: validates amount > 0
        and <= balance, creates a DebtPayment record, updates balance and
        status, and appends a communication note."""
        Debt = self._get_model('Debt')
        DebtPayment = self._get_model('DebtPayment')
        payment_amount = money_dec(amount).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if payment_amount <= 0:
            return {"error": "Payment amount must be greater than 0"}
        debt = self._branch_filter(Debt.query.filter_by(id=debt_id), Debt).first()
        if not debt:
            return {"error": f"Debt with ID {debt_id} not found in the active branch"}
        current_balance = money_dec(debt.balance).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if current_balance <= 0:
            return {"error": "This debt is already fully paid"}
        if payment_amount > current_balance:
            return {"error": f"Payment amount {money_plain(payment_amount)} exceeds remaining balance {money_plain(current_balance)}"}

        remaining_balance = (current_balance - payment_amount).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        payment_notes = str(notes).strip() if notes is not None else None

        payment = DebtPayment(
            debt_id=debt.id,
            customer_id=debt.customer_id,
            amount=float(payment_amount),
            notes=payment_notes,
            branch_id=debt.branch_id
        )
        self.db.session.add(payment)

        # Update balance and status (mirrors calculate_debt_status in app.py)
        debt.balance = float(remaining_balance)
        if remaining_balance <= 0:
            debt.status = 'paid'
        elif debt.due_date and datetime.utcnow() > debt.due_date:
            debt.status = 'overdue'
        elif remaining_balance < money_dec(debt.amount):
            debt.status = 'partial'
        else:
            debt.status = 'pending'

        # Track payment in communication notes like app.py does
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
        payment_note = f"Payment of {money_plain(payment_amount)} received (AI agent)"
        if payment_notes:
            payment_note += f" - {payment_notes}"
        existing_notes = debt.communication_notes or ''
        debt.communication_notes = (
            f"{existing_notes}\n[{timestamp}] {payment_note}" if existing_notes
            else f"[{timestamp}] {payment_note}"
        )
        self.db.session.commit()

        return {
            "success": True,
            "debt_id": debt.id,
            "customer_id": debt.customer_id,
            "payment_id": payment.id,
            "amount_paid": money_plain(payment_amount),
            "remaining_balance": money_plain(debt.balance),
            "debt_status": debt.status,
            "changed_fields": ["balance", "status", "communication_notes"]
        }

    def update_delivery_stage(self, delivery_id: int, stage: str) -> Dict[str, Any]:
        """Move a delivery to its next valid stage (any role)."""
        Delivery = self._get_model('Delivery')
        if stage not in DELIVERY_STAGES:
            return {"error": f"Invalid stage '{stage}'. Valid stages: {sorted(DELIVERY_STAGES)}"}
        delivery = self._branch_filter(Delivery.query.filter_by(id=delivery_id), Delivery).first()
        if not delivery:
            return {"error": f"Delivery with ID {delivery_id} not found in the active branch"}
        current_stage = delivery.stage
        if stage == current_stage:
            return {"error": f"Delivery is already in stage '{current_stage}'"}
        if stage not in DELIVERY_STAGE_FLOW.get(current_stage, []):
            return {"error": f"Cannot move delivery from '{current_stage}' to '{stage}'. Allowed next stages: {DELIVERY_STAGE_FLOW.get(current_stage, [])}"}

        now = datetime.utcnow()
        delivery.stage = stage
        if stage == 'packaged' and not delivery.packaged_at:
            delivery.packaged_at = now
        elif stage == 'delivering' and not delivery.out_for_delivery_at:
            delivery.out_for_delivery_at = now
        elif stage == 'delivered':
            delivery.delivered_at = now
        elif stage == 'cancelled':
            delivery.cancelled_at = now
        self.db.session.commit()
        return {
            "success": True,
            "delivery_id": delivery.id,
            "delivery_number": delivery.delivery_number,
            "previous_stage": current_stage,
            "new_stage": stage,
            "updated_at": now.isoformat()
        }

    def _generate_random_suffix(self) -> str:
        """Generate a random suffix for PO numbers"""
        import uuid
        return uuid.uuid4().hex[:6].upper()


def get_all_tools(read_only: bool = False) -> Dict[str, Dict]:
    """Get tool schemas; agent callers receive only safe read-only tools.

    The legacy TOOL_SCHEMAS format is derived from TOOL_METADATA; write tools
    are filtered out via their ``mutates`` flag."""
    if not read_only:
        return TOOL_SCHEMAS
    return {
        name: schema for name, schema in TOOL_SCHEMAS.items()
        if not TOOL_METADATA[name]['mutates']
    }


def create_tools_instance(db, models: Dict) -> AITools:
    """Factory function to create an AITools instance"""
    return AITools(db, models)
