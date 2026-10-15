"""HelloAgents 配置适配 —— 生成 PaperGraph 智能体的默认运行配置."""

from __future__ import annotations

from pathlib import Path

from hello_agents.core.config import Config

from ...settings import get_settings


def papergraph_agent_config() -> Config:
    memory_root = Path(get_settings().data_dir).resolve() / "memory"
    return Config(
        debug=bool(get_settings().debug),
        log_level=str(get_settings().log_level or "INFO"),
        skills_enabled=False,
        skills_auto_register=False,
        subagent_enabled=False,
        todowrite_enabled=False,
        devlog_enabled=False,
        session_enabled=False,
        trace_enabled=False,
        tool_output_max_lines=300,
        tool_output_max_bytes=8000,
        context_window=get_settings().agent_context_window,
        trace_dir=str(memory_root / "traces"),
        session_dir=str(memory_root / "sessions"),
        tool_output_dir=str(memory_root / "tool-output"),
        skills_dir=str(memory_root / "skills"),
        todowrite_persistence_dir=str(memory_root / "todos"),
        devlog_persistence_dir=str(memory_root / "devlogs"),
    )
