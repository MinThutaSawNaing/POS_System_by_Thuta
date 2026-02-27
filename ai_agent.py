"""
AI Agent Module for POS System
Handles communication with APIFree.ai (Gemini 2.5 Flash Lite)
"""

import os
import json
import requests
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field


APIFREE_BASE_URL = "https://api.apifree.ai/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"


@dataclass
class Message:
    role: str  # 'system', 'user', 'assistant'
    content: str
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None


@dataclass
class ToolCall:
    id: str
    function_name: str
    arguments: Dict[str, Any]


@dataclass
class ChatResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict = field(default_factory=dict)
    error: Optional[str] = None


class AIAgent:
    """Core AI Agent for handling chat completions with tool calling"""
    
    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL, db_get_setting=None):
        self.api_key = api_key or os.environ.get("APIFREE_API_KEY", "")
        # If no API key from env, try to get from database via callback
        if not self.api_key and db_get_setting:
            try:
                db_key = db_get_setting('ai_api_key', '')
                if db_key:
                    self.api_key = db_key
                    print(f"[AI Agent] Loaded API key from database")
            except Exception as e:
                print(f"[AI Agent] Error loading API key from database: {e}")
        if self.api_key:
            # Mask and print first/last few chars for debugging
            masked = f"{self.api_key[:8]}...{self.api_key[-4:]}" if len(self.api_key) > 12 else "configured"
            print(f"[AI Agent] API key is configured: {masked}")
        else:
            print(f"[AI Agent] WARNING: No API key configured")
        self.model = model
        self.base_url = APIFREE_BASE_URL
        self.conversation_history: List[Message] = []
        self.tools: List[Dict] = []
        self.tool_functions: Dict[str, Callable] = {}
        
    def register_tool(self, name: str, description: str, parameters: Dict, function: Callable):
        """Register a tool that the AI can call"""
        tool_schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        }
        self.tools.append(tool_schema)
        self.tool_functions[name] = function
        
    def set_system_prompt(self, prompt: str):
        """Set the system prompt for the AI"""
        # Remove any existing system message
        self.conversation_history = [m for m in self.conversation_history if m.role != "system"]
        # Add new system message at the beginning
        self.conversation_history.insert(0, Message(role="system", content=prompt))
        
    def add_user_message(self, content: str):
        """Add a user message to the conversation"""
        self.conversation_history.append(Message(role="user", content=content))
        
    def add_assistant_message(self, content: str, tool_calls: Optional[List[Dict]] = None):
        """Add an assistant message to the conversation"""
        self.conversation_history.append(Message(
            role="assistant", 
            content=content,
            tool_calls=tool_calls
        ))
        
    def add_tool_result(self, tool_call_id: str, content: str):
        """Add a tool result message to the conversation"""
        self.conversation_history.append(Message(
            role="tool",
            content=content,
            tool_call_id=tool_call_id
        ))
        
    def _build_messages_payload(self) -> List[Dict]:
        """Build the messages payload for the API request"""
        payload = []
        for msg in self.conversation_history:
            message_dict = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                message_dict["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                message_dict["tool_call_id"] = msg.tool_call_id
            payload.append(message_dict)
        return payload
        
    def chat(self, message: Optional[str] = None, temperature: float = 0.7, 
             max_tokens: int = 2048, stream: bool = False, 
             tools_override: Optional[List[Dict]] = None,
             retry_count: int = 0, max_retries: int = 3) -> ChatResponse:
        """
        Send a chat completion request to APIFree.ai
        
        Args:
            message: Optional user message to add before sending
            temperature: Controls randomness (0-2)
            max_tokens: Maximum tokens to generate
            stream: Whether to stream the response
            tools_override: Optional list of tools to use instead of all registered tools
            retry_count: Current retry attempt (for internal use)
            max_retries: Maximum number of retries on failure
            
        Returns:
            ChatResponse object containing the AI's response
        """
        if not self.api_key:
            return ChatResponse(
                content="",
                error="API key not configured. Please set APIFREE_API_KEY environment variable."
            )
            
        if message:
            self.add_user_message(message)
            
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        payload = {
            "model": self.model,
            "messages": self._build_messages_payload(),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            "top_p": 1
        }
        
        # Add tools if registered (use override if provided)
        tools_to_send = tools_override if tools_override is not None else self.tools
        if tools_to_send:
            payload["tools"] = tools_to_send
            payload["tool_choice"] = "auto"
            print(f"[AI Agent API] Sending {len(tools_to_send)} tools with request")
            
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            data = response.json()
            
            # Check for API errors
            if "error" in data:
                return ChatResponse(
                    content="",
                    error=f"API Error: {data['error'].get('message', 'Unknown error')}"
                )
                
            # Parse the response
            choice = data.get("choices", [{}])[0]
            message_data = choice.get("message", {})
            content = message_data.get("content", "")
            finish_reason = choice.get("finish_reason", "")
            
            # Debug: log the response structure
            print(f"[AI Agent API] Finish reason: {finish_reason}")
            print(f"[AI Agent API] Message keys: {list(message_data.keys())}")
            
            # Parse tool calls
            tool_calls = []
            raw_tool_calls = message_data.get("tool_calls", [])
            print(f"[AI Agent API] Raw tool calls count: {len(raw_tool_calls)}")
            
            for tc in raw_tool_calls:
                if tc.get("type") == "function":
                    func = tc.get("function", {})
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                        
                    tool_calls.append(ToolCall(
                        id=tc.get("id", ""),
                        function_name=func.get("name", ""),
                        arguments=args
                    ))
                    
            # Add assistant message to history
            self.add_assistant_message(content, raw_tool_calls if raw_tool_calls else None)
            
            return ChatResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=data.get("usage", {})
            )
            
        except requests.exceptions.Timeout:
            if retry_count < max_retries:
                import time
                wait_time = (2 ** retry_count) + 1  # Exponential backoff: 1, 3, 7 seconds
                print(f"[AI Agent API] Timeout, retrying in {wait_time}s... (attempt {retry_count + 1}/{max_retries})")
                time.sleep(wait_time)
                return self.chat(message=None, temperature=temperature, max_tokens=max_tokens, 
                               stream=stream, tools_override=tools_override, 
                               retry_count=retry_count + 1, max_retries=max_retries)
            return ChatResponse(content="", error="Request timed out. Please try again.")
        except requests.exceptions.ConnectionError:
            if retry_count < max_retries:
                import time
                wait_time = (2 ** retry_count) + 1
                print(f"[AI Agent API] Connection error, retrying in {wait_time}s... (attempt {retry_count + 1}/{max_retries})")
                time.sleep(wait_time)
                return self.chat(message=None, temperature=temperature, max_tokens=max_tokens,
                               stream=stream, tools_override=tools_override,
                               retry_count=retry_count + 1, max_retries=max_retries)
            return ChatResponse(content="", error="Connection error. Please check your internet connection.")
        except requests.exceptions.HTTPError as e:
            error_text = e.response.text if hasattr(e.response, 'text') else str(e)
            # Check for rate limit errors
            if e.response.status_code == 429:
                if retry_count < max_retries:
                    import time
                    wait_time = (2 ** retry_count) * 2 + 1  # Longer backoff for rate limits: 3, 7, 15 seconds
                    print(f"[AI Agent API] Rate limited, retrying in {wait_time}s... (attempt {retry_count + 1}/{max_retries})")
                    time.sleep(wait_time)
                    return self.chat(message=None, temperature=temperature, max_tokens=max_tokens,
                                   stream=stream, tools_override=tools_override,
                                   retry_count=retry_count + 1, max_retries=max_retries)
            return ChatResponse(content="", error=f"HTTP Error {e.response.status_code}: {error_text}")
        except Exception as e:
            return ChatResponse(content="", error=f"Unexpected error: {str(e)}")
            
    def execute_tool_calls(self, tool_calls: List[ToolCall]) -> List[Dict]:
        """
        Execute the tool calls and return results
        
        Returns:
            List of dicts with 'tool_call_id', 'function_name', 'result', 'error'
        """
        results = []
        
        for tc in tool_calls:
            if tc.function_name not in self.tool_functions:
                results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function_name,
                    "result": None,
                    "error": f"Tool '{tc.function_name}' not found"
                })
                continue
                
            try:
                func = self.tool_functions[tc.function_name]
                result = func(**tc.arguments)
                results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function_name,
                    "result": result,
                    "error": None
                })
                # Add to conversation history
                self.add_tool_result(tc.id, json.dumps(result) if result else "")
            except Exception as e:
                results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function_name,
                    "result": None,
                    "error": str(e)
                })
                self.add_tool_result(tc.id, json.dumps({"error": str(e)}))
                
        return results
        
    def clear_history(self):
        """Clear conversation history except system prompt"""
        system_messages = [m for m in self.conversation_history if m.role == "system"]
        self.conversation_history = system_messages
        
    def get_conversation_summary(self) -> str:
        """Get a summary of the conversation"""
        summary = []
        for msg in self.conversation_history:
            if msg.role == "system":
                summary.append("System: [System Prompt]")
            elif msg.role == "user":
                summary.append(f"User: {msg.content[:100]}...")
            elif msg.role == "assistant":
                summary.append(f"AI: {msg.content[:100]}...")
            elif msg.role == "tool":
                summary.append(f"Tool Result: {msg.content[:100]}...")
        return "\n".join(summary)


# Singleton instance for the application
_agent_instance: Optional[AIAgent] = None
_db_get_setting = None


def get_agent(db_get_setting=None) -> AIAgent:
    """Get or create the singleton AI agent instance"""
    global _agent_instance, _db_get_setting
    if db_get_setting:
        _db_get_setting = db_get_setting
    if _agent_instance is None:
        _agent_instance = AIAgent(db_get_setting=_db_get_setting)
    return _agent_instance


def reset_agent():
    """Reset the singleton agent instance"""
    global _agent_instance
    _agent_instance = None
