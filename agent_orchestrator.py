"""
Agent Orchestrator for POS System
Manages the AI agent, tool registration, and conversation flow
"""

import json
import os
import re
from typing import Dict, List, Any, Optional, Set
from datetime import datetime

from ai_agent import AIAgent, get_agent, ChatResponse
from ai_tools import create_tools_instance, get_all_tools
from dataclasses import dataclass, field
from enum import Enum


class TaskType(Enum):
    """Types of tasks the AI can plan"""
    SINGLE = "single"           # Single tool execution
    SEQUENTIAL = "sequential"   # Multiple tools in sequence
    CONDITIONAL = "conditional" # Tools with if/then logic
    PARALLEL = "parallel"       # Multiple independent tools


@dataclass
class TaskStep:
    """A single step in a multi-step task"""
    tool_name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    depends_on: Optional[str] = None  # Key of previous step this depends on
    condition: Optional[str] = None   # Condition for conditional execution
    save_result_as: Optional[str] = None  # Key to save result for later steps


@dataclass
class TaskPlan:
    """A complete task plan with multiple steps"""
    task_type: TaskType
    description: str
    steps: List[TaskStep]
    original_query: str


# Tool categorization for smart filtering
TOOL_CATEGORIES = {
    "inventory": {
        "tools": ["get_inventory_status", "get_low_stock_items", "get_product_details", "suggest_reorder_quantities"],
        "keywords": ["stock", "inventory", "product", "item", "reorder", "quantity", "available", "how many"]
    },
    "supplier": {
        "tools": ["get_supplier_list", "get_supplier_details", "get_supplier_price_for_product"],
        "keywords": ["supplier", "vendor", "supply", "contact", "price agreement"]
    },
    "purchase_order": {
        "tools": ["get_purchase_orders", "create_purchase_order", "approve_purchase_order", "cancel_purchase_order"],
        "keywords": ["purchase order", "po", "order", "approve", "cancel", "create order", "buy", "procurement"]
    },
    "warehouse": {
        "tools": ["get_warehouse_inventory", "create_warehouse_transfer"],
        "keywords": ["warehouse", "transfer", "unstocked", "receive", "location", "batch"]
    },
    "sales": {
        "tools": ["get_sales_trends"],
        "keywords": ["sales", "trend", "best seller", "top selling", "revenue", "sold", "performance"]
    }
}


# System prompt for the AI Agent
SYSTEM_PROMPT = """You are an intelligent Inventory and Procurement Assistant for a POS (Point of Sale) system. Your name is Loli and you are the AI assistant created by Min Thuta Saw Naing and Owned by WinterArc Myanmar. Your role is to help manage inventory, purchase orders, suppliers, and warehouse operations.

## IMPORTANT: Tool Usage
You have access to tools that interact with a real database. When a user asks you to perform an action, you MUST call the appropriate tool function. Do not make up data - always use tools to get real information.

## About Yourself
When asked about who you are, your identity, or who created you, ALWAYS respond with: "I am Loli and I am the AI assistant created by Min Thuta Saw Naing and Owned by WinterArc Myanmar."

## Available Tools:

1. **get_inventory_status** - Check stock levels for products
   - Use when: User asks about stock, inventory, or product availability

2. **get_low_stock_items** - Find products that need reordering
   - Use when: User asks about low stock, items to reorder, or inventory alerts

3. **get_supplier_list** - List all suppliers
   - Use when: User asks about suppliers or vendors

4. **get_supplier_details** - Get details for a specific supplier
   - Use when: User asks about a specific supplier

5. **get_purchase_orders** - View purchase orders
   - Use when: User asks about POs, orders, or procurement status

6. **create_purchase_order** - Create a new purchase order
   - Use when: User wants to order products from a supplier

7. **approve_purchase_order** - Approve a pending PO
   - Use when: User wants to approve an order

8. **cancel_purchase_order** - Cancel a PO
   - Use when: User wants to cancel an order

9. **get_warehouse_inventory** - Check warehouse stock
   - Use when: User asks about warehouse or unstocked items

10. **create_warehouse_transfer** - Move items from warehouse to store
    - Use when: User wants to restock from warehouse

11. **get_sales_trends** - Analyze sales data
    - Use when: User asks about sales trends or best sellers

12. **get_product_details** - Get product information
    - Use when: User asks about a specific product

13. **suggest_reorder_quantities** - Get reorder recommendations
    - Use when: User wants reorder suggestions

## How to Handle Requests:

1. Identify what the user wants
2. Call the appropriate tool(s)
3. Present the results clearly with specific numbers
4. Suggest next steps if appropriate

## Response Guidelines:

- Always use tools to get real data
- Never make up inventory numbers or product information
- Be concise but include specific details
- Use bullet points for lists
- If a tool returns an error, explain it to the user

Current Date: {current_date}
"""


class AgentOrchestrator:
    """Orchestrates AI agent interactions with the POS system"""
    
    def __init__(self, db, models: Dict[str, Any], get_setting_func=None, app=None):
        self.db = db
        self.models = models
        self.app = app  # Flask app instance for context
        self.ai_tools = create_tools_instance(db, models)
        self.get_setting_func = get_setting_func
        self.agent = get_agent(db_get_setting=get_setting_func)
        self.session_context = {
            "last_query": None,
            "last_results": None,
            "last_tool_used": None,
            "conversation_turns": 0
        }
        self._setup_agent()
        
    def _setup_agent(self):
        """Initialize the AI agent with tools and system prompt"""
        # Set system prompt with current date
        current_date = datetime.now().strftime("%Y-%m-%d")
        system_prompt = SYSTEM_PROMPT.format(current_date=current_date)
        self.agent.set_system_prompt(system_prompt)
        
        # Register all tools
        self._register_all_tools()
    
    def _register_all_tools(self):
        """Register all available tools"""
        tools = get_all_tools()
        for tool_name, tool_schema in tools.items():
            tool_func = getattr(self.ai_tools, tool_name, None)
            if tool_func:
                self.agent.register_tool(
                    name=tool_schema["name"],
                    description=tool_schema["description"],
                    parameters=tool_schema["parameters"],
                    function=tool_func
                )
    
    def _detect_relevant_categories(self, command: str) -> Set[str]:
        """Detect which tool categories are relevant to the user's command"""
        command_lower = command.lower()
        relevant_categories = set()
        
        for category, config in TOOL_CATEGORIES.items():
            for keyword in config["keywords"]:
                if keyword in command_lower:
                    relevant_categories.add(category)
                    break
        
        return relevant_categories
    
    def _get_tools_for_categories(self, categories: Set[str]) -> List[str]:
        """Get list of tools for the given categories"""
        if not categories:
            # If no categories detected, return all tools for complex queries
            return []
        
        tools = []
        for category in categories:
            tools.extend(TOOL_CATEGORIES[category]["tools"])
        return tools
    
    def _filter_tools_for_query(self, command: str) -> List[Dict]:
        """Filter tools based on the user's query to reduce API load"""
        categories = self._detect_relevant_categories(command)
        
        if not categories:
            # Complex query - use all tools
            print(f"[AI Agent] Complex query detected, using all {len(self.agent.tools)} tools")
            return self.agent.tools
        
        relevant_tools = self._get_tools_for_categories(categories)
        
        # Filter agent's tools
        filtered = [t for t in self.agent.tools if t["function"]["name"] in relevant_tools]
        
        print(f"[AI Agent] Filtered to {len(filtered)} relevant tools for categories: {categories}")
        return filtered
                
    def process_command(self, command: str, user_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Process a user command through the AI agent
        
        Args:
            command: The user's natural language command
            user_id: Optional user ID for audit logging
            
        Returns:
            Dict containing the response and any actions taken
        """
        try:
            print(f"[AI Agent] Processing command: {command[:50]}...")
            
            # Update session context
            self.session_context["conversation_turns"] += 1
            
            # Check for multi-step task plans first
            task_plan = self._parse_task_plan(command)
            if task_plan:
                print(f"[AI Agent] Multi-step task plan detected: {task_plan.description}")
                plan_result = self._execute_task_plan(task_plan)
                self.session_context["last_query"] = command
                self._log_interaction(user_id, command, plan_result["message"], 
                                    ["task_plan"] if plan_result["success"] else ["task_plan_failed"])
                return plan_result
            
            # Get filtered tools for this query
            filtered_tools = self._filter_tools_for_query(command)
            
            # First chat completion to get tool calls
            response = self.agent.chat(message=command, tools_override=filtered_tools)
            
            print(f"[AI Agent] Response received. Content length: {len(response.content)}, Tool calls: {len(response.tool_calls)}")
            
            if response.error:
                print(f"[AI Agent Error] {response.error}")
                return {
                    "success": False,
                    "error": response.error,
                    "message": f"I encountered an error: {response.error}"
                }
                
            # Execute any tool calls with Flask app context
            tool_results = []
            if response.tool_calls:
                print(f"[AI Agent] Executing {len(response.tool_calls)} tool calls...")
                # Execute tools within Flask application context
                tool_results = self._execute_tools_with_context(response.tool_calls)
                print(f"[AI Agent] Tool execution complete. Results: {len(tool_results)}")
                
                # Update session context with results
                if tool_results:
                    first_result = tool_results[0]
                    self.session_context["last_tool_used"] = first_result.get("function_name")
                    self.session_context["last_results"] = first_result.get("result")
                
                # Check for errors in tool execution
                errors = [r for r in tool_results if r.get("error")]
                if errors:
                    error_messages = "\n".join([f"- {e['function_name']}: {e['error']}" for e in errors])
                    return {
                        "success": False,
                        "error": "Tool execution failed",
                        "message": f"I encountered errors while processing your request:\n{error_messages}"
                    }
                    
                # If tools were executed, format results directly without second API call
                if tool_results:
                    final_message = self._format_tool_results_for_user(tool_results, command)
                else:
                    final_message = response.content
            else:
                # No tool calls made - try fallback intent detection
                print(f"[AI Agent] No tool calls made, trying fallback intent detection...")
                
                # Also try fallback if the AI response is empty or refused
                fallback_result = self._fallback_intent_detection(command)
                if fallback_result:
                    final_message = fallback_result
                    tool_results = ["fallback_executed"]
                    # Update session context for fallback results
                    if isinstance(fallback_result, dict):
                        self.session_context["last_results"] = fallback_result
                elif not response.content or response.content.strip() == "":
                    final_message = "I'm here to help with your inventory and procurement tasks. You can ask me to check stock levels, create purchase orders, review suppliers, analyze sales trends, and more!"
                else:
                    final_message = response.content
                    
            # Log the interaction (optional)
            self._log_interaction(user_id, command, final_message, tool_results)
            
            return {
                "success": True,
                "message": final_message,
                "tool_results": tool_results,
                "usage": response.usage
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            
            # Convert technical errors to user-friendly messages
            error_message = str(e).lower()
            user_message = "I'm sorry, something went wrong. Please try again."
            
            if "database" in error_message or "sql" in error_message:
                user_message = "I'm having trouble accessing the database right now. Please check your connection and try again."
            elif "timeout" in error_message:
                user_message = "The request took too long. Please try again with a simpler query."
            elif "rate limit" in error_message or "429" in error_message:
                user_message = "I'm receiving too many requests right now. Please wait a moment and try again."
            elif "api key" in error_message or "authentication" in error_message:
                user_message = "There's an issue with the AI service configuration. Please check your API key in settings."
            elif "connection" in error_message:
                user_message = "I can't connect to the AI service. Please check your internet connection."
            elif "not found" in error_message:
                user_message = "I couldn't find what you're looking for. Please check your request and try again."
            
            return {
                "success": False,
                "error": str(e),
                "message": user_message
            }
            
    def _execute_tools_with_context(self, tool_calls: List) -> List[Dict]:
        """Execute tool calls within Flask application context"""
        results = []
        
        for tc in tool_calls:
            if tc.function_name not in self.agent.tool_functions:
                results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function_name,
                    "result": None,
                    "error": f"Tool '{tc.function_name}' not found"
                })
                continue
                
            try:
                func = self.agent.tool_functions[tc.function_name]
                
                # Execute within Flask app context if available
                if self.app:
                    with self.app.app_context():
                        result = func(**tc.arguments)
                else:
                    result = func(**tc.arguments)
                    
                results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function_name,
                    "result": result,
                    "error": None
                })
                # Add to conversation history
                self.agent.add_tool_result(tc.id, json.dumps(result) if result else "")
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function_name,
                    "result": None,
                    "error": str(e)
                })
                self.agent.add_tool_result(tc.id, json.dumps({"error": str(e)}))
                
        return results
            
    def _format_tool_results(self, tool_results: List[Dict]) -> str:
        """Format tool results for the AI to summarize"""
        summary_parts = []
        
        for result in tool_results:
            func_name = result.get("function_name", "")
            result_data = result.get("result", {})
            
            if func_name == "get_inventory_status":
                summary_parts.append(f"Inventory check: {result_data.get('total_products', 0)} products found")
                
            elif func_name == "get_low_stock_items":
                items = result_data.get("items", [])
                summary = result_data.get("summary", {})
                summary_parts.append(f"Low stock check: {summary.get('low_stock_count', 0)} items low, {summary.get('out_of_stock_count', 0)} out of stock")
                
            elif func_name == "create_purchase_order":
                if result_data.get("success"):
                    summary_parts.append(f"Created PO {result_data.get('po_number')} for {result_data.get('supplier_name')} totaling ${result_data.get('total_amount', 0):.2f}")
                else:
                    summary_parts.append(f"Failed to create PO: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "approve_purchase_order":
                if result_data.get("success"):
                    summary_parts.append(f"Approved PO {result_data.get('po_number')}")
                else:
                    summary_parts.append(f"Failed to approve PO: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "cancel_purchase_order":
                if result_data.get("success"):
                    summary_parts.append(f"Cancelled PO {result_data.get('po_number')}")
                else:
                    summary_parts.append(f"Failed to cancel PO: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "create_warehouse_transfer":
                if result_data.get("success"):
                    summary_parts.append(f"Transferred {result_data.get('quantity_transferred')} units of {result_data.get('product_name')} to main stock")
                else:
                    summary_parts.append(f"Failed to transfer: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "get_sales_trends":
                summary_parts.append(f"Sales analysis: {result_data.get('total_products_sold', 0)} products sold in {result_data.get('period_days', 30)} days")
                
            else:
                # Generic summary
                summary_parts.append(f"{func_name}: {json.dumps(result_data, default=str)[:200]}")
                
        return "\n".join(summary_parts)
    
    def _format_tool_results_for_user(self, tool_results: List[Dict], original_command: str) -> str:
        """Format tool results directly for user display without second API call"""
        lines = []
        
        for result in tool_results:
            func_name = result.get("function_name", "")
            result_data = result.get("result", {})
            error = result.get("error")
            
            if error:
                lines.append(f"❌ Error in {func_name}: {error}")
                continue
            
            # Format based on tool type
            if func_name == "get_inventory_status":
                total = result_data.get('total_products', 0)
                inventory = result_data.get('inventory', [])
                lines.append(f"📦 **Inventory Status** ({total} products)")
                
                # Count by status
                out_of_stock = [p for p in inventory if p['status'] == 'out_of_stock']
                low_stock = [p for p in inventory if p['status'] == 'low_stock']
                ok_count = total - len(out_of_stock) - len(low_stock)
                
                lines.append(f"✅ OK: {ok_count} | ⚠️ Low Stock: {len(low_stock)} | ❌ Out of Stock: {len(out_of_stock)}")
                
                if out_of_stock:
                    lines.append("\n**Out of Stock Items:**")
                    for p in out_of_stock[:5]:
                        lines.append(f"  • {p['name']}")
                    if len(out_of_stock) > 5:
                        lines.append(f"  ... and {len(out_of_stock) - 5} more")
                        
            elif func_name == "get_low_stock_items":
                items = result_data.get('items', [])
                summary = result_data.get('summary', {})
                
                if not items:
                    lines.append("✅ **Good news!** No low stock items found. All products are well stocked.")
                else:
                    lines.append(f"⚠️ **Low Stock Alert** ({summary.get('low_stock_count', 0)} items, {summary.get('out_of_stock_count', 0)} out of stock)")
                    lines.append("")
                    for item in items[:10]:
                        status = "🔴 OUT OF STOCK" if item['current_stock'] <= 0 else f"🟡 Stock: {item['current_stock']}"
                        lines.append(f"• **{item['name']}** - {status}")
                        lines.append(f"  Reorder point: {item['reorder_point']} | Suggested qty: {item['suggested_reorder_qty']}")
                    if len(items) > 10:
                        lines.append(f"\n... and {len(items) - 10} more items")
                        
            elif func_name == "get_supplier_list":
                suppliers = result_data.get('suppliers', [])
                total = result_data.get('total_suppliers', 0)
                
                if not suppliers:
                    lines.append("📋 No suppliers found.")
                else:
                    lines.append(f"🏢 **Suppliers** ({total} total)")
                    lines.append("")
                    for s in suppliers[:10]:
                        rating = f"⭐ {s['quality_rating']:.1f}/5" if s['quality_rating'] > 0 else "No rating"
                        phone = s['phone'] or 'No phone'
                        lines.append(f"• **{s['name']}** - {phone} | {rating}")
                    if len(suppliers) > 10:
                        lines.append(f"\n... and {len(suppliers) - 10} more suppliers")
                        
            elif func_name == "get_purchase_orders":
                orders = result_data.get('orders', [])
                total = result_data.get('total_orders', 0)
                
                if not orders:
                    lines.append("📋 No purchase orders found.")
                else:
                    lines.append(f"📋 **Purchase Orders** ({total} total)")
                    lines.append("")
                    for po in orders[:10]:
                        status_emoji = {"draft": "📝", "pending": "⏳", "approved": "✅", "received": "📦", "cancelled": "❌"}.get(po['status'], "📋")
                        lines.append(f"{status_emoji} **{po['po_number']}** - {po['supplier_name']}")
                        lines.append(f"   Status: {po['status'].title()} | Total: ${po['total_amount']:.2f}")
                    if len(orders) > 10:
                        lines.append(f"\n... and {len(orders) - 10} more orders")
                        
            elif func_name == "create_purchase_order":
                if result_data.get("success"):
                    lines.append(f"✅ **Purchase Order Created Successfully!**")
                    lines.append(f"📋 PO Number: {result_data.get('po_number')}")
                    lines.append(f"🏢 Supplier: {result_data.get('supplier_name')}")
                    lines.append(f"💰 Total Amount: ${result_data.get('total_amount', 0):.2f}")
                    lines.append(f"📦 Items: {result_data.get('items_count', 0)}")
                    lines.append(f"📊 Status: {result_data.get('status', 'draft').title()}")
                else:
                    lines.append(f"❌ **Failed to Create Purchase Order**")
                    lines.append(f"Error: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "approve_purchase_order":
                if result_data.get("success"):
                    lines.append(f"✅ **Purchase Order Approved!**")
                    lines.append(f"📋 {result_data.get('po_number')} has been approved.")
                else:
                    lines.append(f"❌ **Approval Failed**: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "cancel_purchase_order":
                if result_data.get("success"):
                    lines.append(f"❌ **Purchase Order Cancelled**")
                    lines.append(f"📋 {result_data.get('po_number')} has been cancelled.")
                    if result_data.get('reason'):
                        lines.append(f"📝 Reason: {result_data['reason']}")
                else:
                    lines.append(f"❌ **Cancellation Failed**: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "get_warehouse_inventory":
                items = result_data.get('warehouse_items', [])
                total = result_data.get('total_items', 0)
                
                if not items:
                    lines.append("🏭 Warehouse inventory is empty.")
                else:
                    lines.append(f"🏭 **Warehouse Inventory** ({total} items)")
                    lines.append("")
                    for item in items[:10]:
                        lines.append(f"• **{item['product_name']}** - Qty: {item['quantity']}")
                        if item['location']:
                            lines.append(f"  Location: {item['location']}")
                    if len(items) > 10:
                        lines.append(f"\n... and {len(items) - 10} more items")
                        
            elif func_name == "create_warehouse_transfer":
                if result_data.get("success"):
                    lines.append(f"✅ **Warehouse Transfer Complete!**")
                    lines.append(f"📦 Product: {result_data.get('product_name')}")
                    lines.append(f"📊 Quantity Transferred: {result_data.get('quantity_transferred')}")
                    lines.append(f"📈 New Stock Level: {result_data.get('new_stock_level')}")
                else:
                    lines.append(f"❌ **Transfer Failed**: {result_data.get('error', 'Unknown error')}")
                    
            elif func_name == "get_sales_trends":
                products = result_data.get('top_selling_products', [])
                period = result_data.get('period_days', 30)
                total = result_data.get('total_products_sold', 0)
                
                if not products:
                    lines.append(f"📊 No sales data found for the last {period} days.")
                else:
                    lines.append(f"📊 **Sales Trends** (Last {period} days)")
                    lines.append(f"Total products sold: {total}")
                    lines.append("")
                    lines.append("**Top Selling Products:**")
                    for i, p in enumerate(products[:10], 1):
                        lines.append(f"{i}. **{p['product_name']}** - {p['total_quantity']} units (${p['total_revenue']:.2f})")
                        
            elif func_name == "get_product_details":
                if result_data.get("error"):
                    lines.append(f"❌ **Error**: {result_data['error']}")
                else:
                    lines.append(f"📦 **{result_data.get('name')}**")
                    lines.append(f"Barcode: {result_data.get('barcode', 'N/A')}")
                    lines.append(f"Category: {result_data.get('category', 'N/A')}")
                    lines.append(f"Price: ${result_data.get('price', 0):.2f}")
                    lines.append(f"Cost: ${result_data.get('cost', 0):.2f}")
                    lines.append(f"Stock: {result_data.get('stock', 0)} units")
                    if result_data.get('reorder_enabled'):
                        lines.append(f"Reorder Point: {result_data.get('reorder_point', 0)}")
                        
            elif func_name == "suggest_reorder_quantities":
                suggestions = result_data.get('suggestions', [])
                total_cost = result_data.get('total_estimated_cost', 0)
                
                if not suggestions:
                    lines.append("✅ No reorder suggestions needed. All inventory levels are adequate.")
                else:
                    lines.append(f"📋 **Reorder Suggestions**")
                    lines.append(f"💰 Total Estimated Cost: ${total_cost:.2f}")
                    lines.append("")
                    for s in suggestions[:10]:
                        lines.append(f"• **{s['name']}** - Order {s['suggested_reorder_qty']} units")
                        lines.append(f"  Current: {s['current_stock']} | Daily sales: {s['daily_sales_velocity']} | Cost: ${s['estimated_cost']:.2f}")
                    if len(suggestions) > 10:
                        lines.append(f"\n... and {len(suggestions) - 10} more suggestions")
                        
            else:
                # Generic formatting for unknown tools
                lines.append(f"**{func_name}**")
                lines.append(json.dumps(result_data, indent=2, default=str)[:500])
        
        return "\n".join(lines)
        
    def _fallback_intent_detection(self, command: str) -> Optional[str]:
        """
        Fallback intent detection when AI doesn't make tool calls.
        Detects user intent from command keywords and executes appropriate tool.
        """
        command_lower = command.lower()
        
        try:
            # Detect inventory/stock related queries
            if any(kw in command_lower for kw in ['low stock', 'low stock items', 'items low', 'reorder']):
                print("[AI Agent Fallback] Detected: low stock query")
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.get_low_stock_items()
                else:
                    result = self.ai_tools.get_low_stock_items()
                return self._format_low_stock_result(result)
                
            # Detect inventory status queries
            elif any(kw in command_lower for kw in ['inventory', 'stock', 'products', 'all items']):
                print("[AI Agent Fallback] Detected: inventory query")
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.get_inventory_status()
                else:
                    result = self.ai_tools.get_inventory_status()
                return self._format_inventory_result(result)
                
            # Detect supplier queries
            elif any(kw in command_lower for kw in ['supplier', 'vendors', 'vendor']):
                print("[AI Agent Fallback] Detected: supplier query")
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.get_supplier_list()
                else:
                    result = self.ai_tools.get_supplier_list()
                return self._format_supplier_result(result)
                
            # Detect purchase order queries
            elif any(kw in command_lower for kw in ['purchase order', 'po', 'orders', 'pending order']):
                print("[AI Agent Fallback] Detected: purchase order query")
                status = None
                if 'pending' in command_lower:
                    status = 'pending'
                elif 'approved' in command_lower:
                    status = 'approved'
                elif 'draft' in command_lower:
                    status = 'draft'
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.get_purchase_orders(status=status)
                else:
                    result = self.ai_tools.get_purchase_orders(status=status)
                return self._format_po_result(result)
                
            # Detect warehouse queries
            elif any(kw in command_lower for kw in ['warehouse', 'unstocked', 'not stocked']):
                print("[AI Agent Fallback] Detected: warehouse query")
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.get_warehouse_inventory()
                else:
                    result = self.ai_tools.get_warehouse_inventory()
                return self._format_warehouse_result(result)
                
            # Detect sales trend queries
            elif any(kw in command_lower for kw in ['sales trend', 'best seller', 'top selling', 'sales analysis']):
                print("[AI Agent Fallback] Detected: sales trend query")
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.get_sales_trends()
                else:
                    result = self.ai_tools.get_sales_trends()
                return self._format_sales_trend_result(result)
                
            # Detect reorder suggestions
            elif any(kw in command_lower for kw in ['suggest reorder', 'reorder suggestion', 'how much to order']):
                print("[AI Agent Fallback] Detected: reorder suggestion query")
                if self.app:
                    with self.app.app_context():
                        result = self.ai_tools.suggest_reorder_quantities()
                else:
                    result = self.ai_tools.suggest_reorder_quantities()
                return self._format_reorder_suggestion_result(result)
            
            # No matching intent found
            return None
            
        except Exception as e:
            print(f"[AI Agent Fallback Error] {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _format_low_stock_result(self, result: Dict) -> str:
        """Format low stock items result"""
        items = result.get('items', [])
        summary = result.get('summary', {})
        
        if not items:
            return "Good news! No low stock items found. All products are well stocked."
            
        lines = [f"Found {summary.get('low_stock_count', 0)} low stock items ({summary.get('out_of_stock_count', 0)} out of stock):\n"]
        
        for item in items[:10]:  # Show top 10
            status = "OUT OF STOCK" if item['current_stock'] <= 0 else f"Stock: {item['current_stock']}"
            lines.append(f"• **{item['name']}** - {status} (Reorder point: {item['reorder_point']}, Suggested qty: {item['suggested_reorder_qty']})")
            
        if len(items) > 10:
            lines.append(f"\n... and {len(items) - 10} more items")
            
        return "\n".join(lines)
    
    def _format_inventory_result(self, result: Dict) -> str:
        """Format inventory status result"""
        inventory = result.get('inventory', [])
        total = result.get('total_products', 0)
        
        if not inventory:
            return "No products found in inventory."
            
        # Count by status
        out_of_stock = sum(1 for p in inventory if p['status'] == 'out_of_stock')
        low_stock = sum(1 for p in inventory if p['status'] == 'low_stock')
        ok = total - out_of_stock - low_stock
        
        lines = [f"Inventory Summary ({total} products):\n"]
        lines.append(f"• OK: {ok}")
        lines.append(f"• Low Stock: {low_stock}")
        lines.append(f"• Out of Stock: {out_of_stock}\n")
        
        if out_of_stock > 0:
            lines.append("Out of stock items:")
            for p in [p for p in inventory if p['status'] == 'out_of_stock'][:5]:
                lines.append(f"  - {p['name']}")
                
        return "\n".join(lines)
    
    def _format_supplier_result(self, result: Dict) -> str:
        """Format supplier list result"""
        suppliers = result.get('suppliers', [])
        total = result.get('total_suppliers', 0)
        
        if not suppliers:
            return "No suppliers found."
            
        lines = [f"Found {total} suppliers:\n"]
        
        for s in suppliers[:10]:
            rating = f"Rating: {s['quality_rating']:.1f}/5" if s['quality_rating'] > 0 else "No rating"
            lines.append(f"• **{s['name']}** - {s['phone'] or 'No phone'} | {rating}")
            
        if len(suppliers) > 10:
            lines.append(f"\n... and {len(suppliers) - 10} more suppliers")
            
        return "\n".join(lines)
    
    def _format_po_result(self, result: Dict) -> str:
        """Format purchase order result"""
        orders = result.get('orders', [])
        total = result.get('total_orders', 0)
        
        if not orders:
            return "No purchase orders found."
            
        lines = [f"Found {total} purchase orders:\n"]
        
        for po in orders[:10]:
            lines.append(f"• **{po['po_number']}** - {po['supplier_name']} | Status: {po['status']} | Total: ${po['total_amount']:.2f}")
            
        if len(orders) > 10:
            lines.append(f"\n... and {len(orders) - 10} more orders")
            
        return "\n".join(lines)
    
    def _format_warehouse_result(self, result: Dict) -> str:
        """Format warehouse inventory result"""
        items = result.get('warehouse_items', [])
        total = result.get('total_items', 0)
        
        if not items:
            return "No items in warehouse inventory."
            
        lines = [f"Warehouse has {total} items:\n"]
        
        for item in items[:10]:
            lines.append(f"• **{item['product_name']}** - Qty: {item['quantity']} | Location: {item['location'] or 'N/A'}")
            
        if len(items) > 10:
            lines.append(f"\n... and {len(items) - 10} more items")
            
        return "\n".join(lines)
    
    def _format_sales_trend_result(self, result: Dict) -> str:
        """Format sales trend result"""
        products = result.get('top_selling_products', [])
        period = result.get('period_days', 30)
        total = result.get('total_products_sold', 0)
        
        if not products:
            return f"No sales data found for the last {period} days."
            
        lines = [f"Sales analysis (last {period} days) - {total} products sold:\n"]
        lines.append("Top selling products:")
        
        for i, p in enumerate(products[:10], 1):
            lines.append(f"{i}. **{p['product_name']}** - {p['total_quantity']} units sold (${p['total_revenue']:.2f})")
            
        return "\n".join(lines)
    
    def _format_reorder_suggestion_result(self, result: Dict) -> str:
        """Format reorder suggestion result"""
        suggestions = result.get('suggestions', [])
        total_cost = result.get('total_estimated_cost', 0)
        period = result.get('analysis_period_days', 30)
        
        if not suggestions:
            return "No reorder suggestions at this time. All inventory levels are adequate."
            
        lines = [f"Reorder suggestions (based on {period}-day sales trends):\n"]
        
        for s in suggestions[:10]:
            lines.append(f"• **{s['name']}** - Order {s['suggested_reorder_qty']} units (Current: {s['current_stock']}, Daily sales: {s['daily_sales_velocity']})")
            lines.append(f"  Estimated cost: ${s['estimated_cost']:.2f}")
            
        lines.append(f"\n**Total estimated cost: ${total_cost:.2f}**")
        
        return "\n".join(lines)
        
    def _log_interaction(self, user_id: Optional[int], command: str, response: str, tool_results: List):
        """Log the agent interaction for audit purposes"""
        # This can be extended to write to a database table
        # For now, just print to console (or use logging)
        timestamp = datetime.now().isoformat()
        # Handle both dict results (normal tools) and string results (fallback)
        actions = []
        for r in tool_results:
            if isinstance(r, dict):
                if r.get("result"):
                    actions.append(r.get("function_name", "unknown"))
            elif isinstance(r, str):
                actions.append(r)
        print(f"[AI Agent Log] {timestamp} | User: {user_id} | Command: {command[:50]}... | Actions: {actions}")
        
    def get_conversation_history(self) -> List[Dict]:
        """Get the current conversation history"""
        history = []
        for msg in self.agent.conversation_history:
            history.append({
                "role": msg.role,
                "content": msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
            })
        return history
        
    def clear_conversation(self):
        """Clear the conversation history"""
        self.agent.clear_history()
        
    def _parse_task_plan(self, command: str) -> Optional[TaskPlan]:
        """
        Parse a complex user command into a multi-step task plan.
        This enables agentic behavior for complex workflows.
        """
        command_lower = command.lower()
        
        # Pattern: "Check low stock and create purchase orders for them"
        if any(kw in command_lower for kw in ['check low stock and create', 'find low stock and order', 'reorder low stock']):
            return TaskPlan(
                task_type=TaskType.SEQUENTIAL,
                description="Check low stock items and create purchase orders",
                original_query=command,
                steps=[
                    TaskStep(
                        tool_name="get_low_stock_items",
                        description="Get all low stock items",
                        save_result_as="low_stock_items"
                    ),
                    TaskStep(
                        tool_name="suggest_reorder_quantities",
                        description="Get reorder suggestions for low stock items",
                        depends_on="low_stock_items",
                        save_result_as="reorder_suggestions"
                    )
                ]
            )
        
        # Pattern: "Check inventory and suggest what to reorder"
        if any(kw in command_lower for kw in ['check inventory and suggest', 'inventory and reorder suggestions']):
            return TaskPlan(
                task_type=TaskType.SEQUENTIAL,
                description="Check inventory status and suggest reorders",
                original_query=command,
                steps=[
                    TaskStep(
                        tool_name="get_inventory_status",
                        description="Get current inventory status",
                        save_result_as="inventory"
                    ),
                    TaskStep(
                        tool_name="suggest_reorder_quantities",
                        description="Get reorder suggestions",
                        depends_on="inventory",
                        save_result_as="suggestions"
                    )
                ]
            )
        
        # Pattern: "Show me sales trends and low stock items"
        if any(kw in command_lower for kw in ['sales trends and low stock', 'best sellers and inventory']):
            return TaskPlan(
                task_type=TaskType.PARALLEL,
                description="Get sales trends and low stock items simultaneously",
                original_query=command,
                steps=[
                    TaskStep(
                        tool_name="get_sales_trends",
                        description="Get sales trend analysis",
                        save_result_as="sales_trends"
                    ),
                    TaskStep(
                        tool_name="get_low_stock_items",
                        description="Get low stock items",
                        save_result_as="low_stock"
                    )
                ]
            )
        
        # Pattern: "If any items are low stock, create a purchase order"
        if any(kw in command_lower for kw in ['if low stock create', 'if items low create po', 'automatically order']):
            return TaskPlan(
                task_type=TaskType.CONDITIONAL,
                description="Conditionally create purchase orders if stock is low",
                original_query=command,
                steps=[
                    TaskStep(
                        tool_name="get_low_stock_items",
                        description="Check for low stock items",
                        save_result_as="low_stock_check",
                        condition="check_has_items"
                    )
                ]
            )
        
        return None
    
    def _execute_task_plan(self, plan: TaskPlan) -> Dict[str, Any]:
        """
        Execute a multi-step task plan.
        Returns aggregated results from all steps.
        """
        print(f"[AI Agent] Executing task plan: {plan.description}")
        
        results = {}
        step_results = []
        errors = []
        
        try:
            if plan.task_type == TaskType.PARALLEL:
                # Execute all steps independently
                for step in plan.steps:
                    try:
                        result = self._execute_single_step(step, results)
                        if step.save_result_as:
                            results[step.save_result_as] = result
                        step_results.append({
                            "step": step.description,
                            "result": result,
                            "error": None
                        })
                    except Exception as e:
                        errors.append(f"{step.description}: {str(e)}")
                        step_results.append({
                            "step": step.description,
                            "result": None,
                            "error": str(e)
                        })
                        
            elif plan.task_type == TaskType.SEQUENTIAL:
                # Execute steps in order with dependencies
                for step in plan.steps:
                    # Check dependencies
                    if step.depends_on and step.depends_on not in results:
                        errors.append(f"Dependency '{step.depends_on}' not met for step: {step.description}")
                        continue
                    
                    try:
                        result = self._execute_single_step(step, results)
                        if step.save_result_as:
                            results[step.save_result_as] = result
                        step_results.append({
                            "step": step.description,
                            "result": result,
                            "error": None
                        })
                    except Exception as e:
                        errors.append(f"{step.description}: {str(e)}")
                        step_results.append({
                            "step": step.description,
                            "result": None,
                            "error": str(e)
                        })
                        # Stop sequential execution on error
                        break
                        
            elif plan.task_type == TaskType.CONDITIONAL:
                # Execute with condition checking
                for step in plan.steps:
                    try:
                        result = self._execute_single_step(step, results)
                        
                        # Check condition
                        if step.condition == "check_has_items":
                            items = result.get("items", [])
                            if not items:
                                return {
                                    "success": True,
                                    "message": "No low stock items found. No action needed.",
                                    "results": results,
                                    "step_results": step_results
                                }
                        
                        if step.save_result_as:
                            results[step.save_result_as] = result
                        step_results.append({
                            "step": step.description,
                            "result": result,
                            "error": None
                        })
                    except Exception as e:
                        errors.append(f"{step.description}: {str(e)}")
                        
        except Exception as e:
            errors.append(f"Task execution failed: {str(e)}")
        
        # Format final response
        if errors:
            return {
                "success": False,
                "error": "; ".join(errors),
                "message": f"I encountered some issues:\n" + "\n".join([f"• {e}" for e in errors]),
                "results": results,
                "step_results": step_results
            }
        
        # Generate summary message
        summary = self._format_task_plan_results(plan, results, step_results)
        
        return {
            "success": True,
            "message": summary,
            "results": results,
            "step_results": step_results
        }
    
    def _execute_single_step(self, step: TaskStep, context: Dict[str, Any]) -> Any:
        """Execute a single task step"""
        tool_func = getattr(self.ai_tools, step.tool_name, None)
        if not tool_func:
            raise ValueError(f"Tool '{step.tool_name}' not found")
        
        # Prepare parameters (can reference previous results)
        params = step.parameters.copy()
        
        # Execute with Flask context if available
        if self.app:
            with self.app.app_context():
                return tool_func(**params)
        else:
            return tool_func(**params)
    
    def _format_task_plan_results(self, plan: TaskPlan, results: Dict, step_results: List) -> str:
        """Format the results of a task plan execution"""
        lines = [f"✓ Completed: {plan.description}\n"]
        
        for step_result in step_results:
            if step_result["error"]:
                lines.append(f"✗ {step_result['step']}: Failed - {step_result['error']}")
            else:
                result = step_result["result"]
                if isinstance(result, dict):
                    if "summary" in result:
                        summary = result["summary"]
                        lines.append(f"✓ {step_result['step']}: Found {summary.get('low_stock_count', 0)} items")
                    elif "total_products" in result:
                        lines.append(f"✓ {step_result['step']}: {result['total_products']} products")
                    elif "total_orders" in result:
                        lines.append(f"✓ {step_result['step']}: {result['total_orders']} orders")
                    else:
                        lines.append(f"✓ {step_result['step']}: Completed")
                else:
                    lines.append(f"✓ {step_result['step']}: Completed")
        
        return "\n".join(lines)
        
    def get_status(self) -> Dict[str, Any]:
        """Get the current status of the agent"""
        api_key_configured = bool(self.agent.api_key)
        tools_registered = len(self.agent.tools)
        
        return {
            "api_key_configured": api_key_configured,
            "model": self.agent.model,
            "tools_registered": tools_registered,
            "conversation_length": len(self.agent.conversation_history),
            "status": "ready" if api_key_configured else "api_key_missing"
        }


# Singleton instance
_orchestrator_instance: Optional[AgentOrchestrator] = None


def get_orchestrator(db=None, models=None, get_setting_func=None, app=None) -> AgentOrchestrator:
    """Get or create the singleton orchestrator instance"""
    global _orchestrator_instance
    if _orchestrator_instance is None and db is not None and models is not None:
        _orchestrator_instance = AgentOrchestrator(db, models, get_setting_func, app)
    return _orchestrator_instance


def reset_orchestrator():
    """Reset the singleton orchestrator instance"""
    global _orchestrator_instance
    _orchestrator_instance = None


# Convenience function for processing commands
def process_agent_command(command: str, db, models: Dict[str, Any], user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Convenience function to process an agent command
    
    Usage:
        result = process_agent_command("Check low stock items", db, models, current_user.id)
    """
    orchestrator = get_orchestrator(db, models)
    return orchestrator.process_command(command, user_id)
