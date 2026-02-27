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
SYSTEM_PROMPT = """You are an intelligent Inventory and Procurement Assistant for a POS (Point of Sale) system. Your role is to help manage inventory, purchase orders, suppliers, and warehouse operations through natural language commands.

## Your Capabilities:

1. **Inventory Management**:
   - Check current stock levels for all products or specific items
   - Identify low stock and out-of-stock items
   - Suggest optimal reorder quantities based on sales trends
   - Get product details including pricing and supplier information

2. **Purchase Order Management**:
   - Create new purchase orders for suppliers
   - View existing purchase orders and their status
   - Approve pending purchase orders
   - Cancel purchase orders with appropriate reasons

3. **Supplier Management**:
   - List all suppliers with their contact information
   - Get detailed supplier information including price agreements
   - Check supplier performance ratings and order history

4. **Warehouse Operations**:
   - Check warehouse inventory (products received but not yet stocked)
   - Transfer products from warehouse to main store stock
   - Track batch numbers and locations

5. **Sales Analysis**:
   - Analyze sales trends to inform reordering decisions
   - Identify top-selling products
   - Calculate sales velocity for inventory planning

## How to Respond:

1. **Analyze the Request**: Understand what the user wants to accomplish
2. **Use Tools**: Call the appropriate tools to gather information or perform actions
3. **Provide Clear Results**: Explain what actions were taken and their outcomes
4. **Be Proactive**: Suggest next steps or related actions when appropriate
5. **Confirm Destructive Actions**: For cancellations or significant changes, ask for confirmation

## Response Format:

- Be concise but informative
- Use bullet points for lists
- Include specific numbers and details
- Highlight important information (low stock items, pending approvals, etc.)
- If you cannot complete a request, explain why and suggest alternatives

## Important Notes:

- You can only perform actions that have available tools
- Always verify product IDs and supplier IDs before taking action
- When creating purchase orders, ensure all items have valid product IDs
- For warehouse transfers, verify sufficient warehouse stock exists
- Purchase orders are created in 'draft' status and need approval

Current Date: {current_date}
"""


class AgentOrchestrator:
    """Orchestrates AI agent interactions with the POS system"""
    
    def __init__(self, db, models: Dict[str, Any], get_setting_func=None):
        self.db = db
        self.models = models
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
            # First chat completion to get tool calls
            response = self.agent.chat(message=command)
            
            if response.error:
                print(f"[AI Agent Error] {response.error}")
                return {
                    "success": False,
                    "error": response.error,
                    "message": f"I encountered an error: {response.error}"
                }
                
            # Execute any tool calls
            tool_results = []
            if response.tool_calls:
                tool_results = self.agent.execute_tool_calls(response.tool_calls)
                
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
            return {
                "success": False,
                "error": str(e),
                "message": f"An unexpected error occurred: {str(e)}"
            }
            
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
        
    def _log_interaction(self, user_id: Optional[int], command: str, response: str, tool_results: List[Dict]):
        """Log the agent interaction for audit purposes"""
        # This can be extended to write to a database table
        # For now, just print to console (or use logging)
        timestamp = datetime.now().isoformat()
        actions = [r.get("function_name") for r in tool_results if r.get("result")]
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


def get_orchestrator(db=None, models=None, get_setting_func=None) -> AgentOrchestrator:
    """Get or create the singleton orchestrator instance"""
    global _orchestrator_instance
    if _orchestrator_instance is None and db is not None and models is not None:
        _orchestrator_instance = AgentOrchestrator(db, models, get_setting_func)
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
