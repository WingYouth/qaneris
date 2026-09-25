"""Agent interface."""

from __future__ import annotations

from dataclasses import dataclass

from smartdata.application.service import SmartDataService
from smartdata.contracts import AskRequest, AskResponse


@dataclass
class AnalysisAgent:
    """Small deterministic agent facade; an LLM planner can replace plan() later."""

    service: SmartDataService

    def plan(self, goal: str) -> list[str]:
        return [
            f"获取与目标相关的总体数据：{goal}",
            "检查数值指标的分布和极值",
            "总结结论并提出后续下钻问题",
        ]

    def run(self, goal: str, workspace_id: str = "default") -> AskResponse:
        return self.service.ask(AskRequest(question=goal, workspace_id=workspace_id))
