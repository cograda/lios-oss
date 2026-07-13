"""Declarative tool DSL for MCP tool generation.

Provides reusable builders that eliminate repetitive handler code
while keeping the same tool names and parameter schemas.
"""

from app.tools.base import CustomTool, ExtraFilter, ToolAnnotations, ToolBuilder
from app.tools.list_tool import ListTool
from app.tools.search_tool import SearchTool
from app.tools.semantic_tool import SemanticSearchTool
from app.tools.stats_tool import StatsTool

__all__ = [
    "CustomTool",
    "ExtraFilter",
    "ListTool",
    "SearchTool",
    "SemanticSearchTool",
    "StatsTool",
    "ToolAnnotations",
    "ToolBuilder",
]
