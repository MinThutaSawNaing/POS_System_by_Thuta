"""
Agent Orchestrator for POS System
Manages the AI agent, tool registration, and conversation flow
"""

import json
import os
from typing import Dict, List, Any, Optional
from datetime import datetime

from ai_agent import AIAgent, get_agent, ChatResponse
from ai_tools import create_tools_instance, get_all_tools


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
        self._setup_agent()
        
    def _setup_agent(self):
        """Initialize the AI agent with tools and system prompt"""
        # Set system prompt with current date
        current_date = datetime.now().strftime("%Y-%m-%d")
        system_prompt = SYSTEM_PROMPT.format(current_date=current_date)
        self.agent.set_system_prompt(system_prompt)
        
        # Register all tools
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
            
            # First chat completion to get tool calls
            response = self.agent.chat(message=command)
            
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
                
                # Check for errors in tool execution
                errors = [r for r in tool_results if r.get("error")]
                if errors:
                    error_messages = "\n".join([f"- {e['function_name']}: {e['error']}" for e in errors])
                    return {
                        "success": False,
                        "error": "Tool execution failed",
                        "message": f"I encountered errors while processing your request:\n{error_messages}"
                    }
                    
                # If tools were executed, get final response from AI
                if tool_results:
                    # Create a summary of tool results for the AI
                    tool_summary = self._format_tool_results(tool_results)
                    follow_up = self.agent.chat(
                        message=f"Based on the tool results:\n{tool_summary}\n\nPlease provide a clear, concise summary of what was accomplished for the user."
                    )
                    final_message = follow_up.content if not follow_up.error else response.content
                else:
                    final_message = response.content
            else:
                # No tool calls made - try fallback intent detection
                print(f"[AI Agent] No tool calls made, trying fallback intent detection...")
                fallback_result = self._fallback_intent_detection(command)
                if fallback_result:
                    final_message = fallback_result
                    tool_results = ["fallback_executed"]
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
            return {
                "success": False,
                "error": str(e),
                "message": f"An unexpected error occurred: {str(e)}"
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
