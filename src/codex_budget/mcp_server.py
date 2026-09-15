from __future__ import annotations

import asyncio

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field

from . import budget as b
from . import popup

server = FastMCP('codex-budget', log_level='ERROR')


class BudgetChoice(BaseModel):
    # Codex accepts the MCP schema subset, which excludes an object-level title.
    model_config = ConfigDict(json_schema_extra=lambda schema: schema.pop("title", None))

    choice: str = Field(title='계속 사용할까요?',
                        json_schema_extra={'enum': ['5%p 추가 사용', '오늘 제한 해제', '오늘은 쉬기']})


def blocked(reason):
    return {'decision': 'block', 'reason': reason}


async def evaluate(prompt, elicit):
    try:
        action = b.prompt_action(prompt)
        config = {**b.DEFAULTS, **b.read_json(b.DATA / 'config.json', {})}
        if not config['enabled'] and action is None:
            return {}
        status = await asyncio.to_thread(b.refresh, fresh=True,
                                         action=action if action and action[0] != 'status' else None,
                                         consume_warnings=action is None)
        if action:
            return blocked(b.line(status) + '\n원래 요청을 다시 입력하세요.')
        if not status['blocked']:
            return {'systemMessage': status['warning']} if status.get('warning') else {}
        if popup.available():
            choice = await asyncio.to_thread(popup.ask, status)
        else:
            answer = await elicit(b.line(status) + '\n오늘 예산에 도달했거나 쉬는 날입니다.', BudgetChoice)
            choice = answer.data.choice if answer.action == 'accept' and answer.data is not None else None
        if choice is None:
            return blocked('선택이 제출되지 않았습니다. 취소·시간 초과 또는 실행 환경의 자동 거절일 수 있습니다.\n'
                           'never 모드에서도 선택하려면 codex-budget-run으로 실행하세요.\n'
                           '직접 입력: 예산 +5 / 예산 해제 / 예산 쉬기')
        current = await asyncio.to_thread(b.refresh, fresh=True)
        if current['date'] != status['date'] or current.get('account') != status.get('account'):
            return blocked('날짜나 계정이 바뀌었습니다. 요청을 다시 보내 새 예산을 확인하세요.')
        actions = {'5%p 추가 사용': ('allow', '5'), '오늘 제한 해제': ('unlimited',), '오늘은 쉬기': ('rest',)}
        if choice not in actions:
            return blocked('알 수 없는 선택입니다. 이 요청은 실행하지 않습니다.')
        status = await asyncio.to_thread(b.refresh, fresh=True, action=actions[choice])
        if status['blocked']:
            return blocked(b.line(status) + '\n요청을 보류했습니다. 다시 요청하면 선택 창이 표시됩니다.')
        return {'systemMessage': b.line(status) + '\n선택을 적용하고 요청을 계속합니다.'}
    except Exception as exc:
        return blocked(f'예산 확인 실패: {exc}\n다시 시도하거나 다른 셸에서 codex-budget disable을 실행하세요.')


@server.tool()
async def budget_gate(prompt: str, ctx: Context) -> dict:
    """Codex UserPromptSubmit hook: check daily quota and ask the user before exceeding it."""
    return await evaluate(prompt, ctx.elicit)


if __name__ == '__main__':
    server.run(transport='stdio')
