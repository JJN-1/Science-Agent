from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.agent_kernel import specs as kernel_specs
from app.api.schemas import ToolOut

router = APIRouter(tags=["tools"])


@router.get("/api/tools", response_model=list[ToolOut])
def list_tools(request: Request):
    """内核可调用的工具清单（US-404）。

    数据源是**运行中的注册表**，不是配置文件：工具是代码资产，配置文件最多能改「谁能用」，
    改不了「有什么」。侧栏据此渲染工具清单与权限徽标，界面显示的权限等级与真正执行时
    判定的权限等级因此是同一个值 —— 两处各存一份，迟早出现「界面写着只读、实际要审批」。
    """
    registry = getattr(request.app.state, "tool_registry", None)
    if registry is None:
        # 注册表在 lifespan 里装配。走到这里说明应用没走完启动流程，
        # 与其返回一个空列表（界面会显示「没有可用工具」，指向完全错误的方向），
        # 不如明确说「还没装配好」。
        raise HTTPException(status_code=503, detail="工具注册表尚未装配")

    return [
        {**row, "allowed_agents": kernel_specs.agents_allowing(row["name"])}
        for row in registry.describe()
    ]
