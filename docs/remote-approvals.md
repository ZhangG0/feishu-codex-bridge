# 飞书单次权限审批

2026-10-29 实施状态：代码与 35 项本地测试通过；飞书应用 1.0.2 发布了长连接 card.action.trigger 回调。审批卡片演示已发至总裁办，真实上传成功。

**尚未完成端到端验收**：管理员身份已通过总裁办唯一口令及当前聊天确认；用户级 PermissionRequest 钩子已安装但 hooks/list 明确返回 untrusted；因此真实桌面权限请求尚不能通过此钩子远程审批。不要将飞书应用已发布等同于审批已生效。

## 权限及关联

- 原任务发起人来自飞书 SDK 消息事件的 sender_id.open_id；管理员名单来自本机 config.json 的 approvals.admin_open_ids。不能由群指令自行修改，不从昵称、群主身份或历史发送次数推断管理员。
- 只有桥接数据库中 running 任务的 thread_id + turn_id 精确对应 PermissionRequest，才发送到原始任务所在群。其他桌面任务维持原有审批。
- 卡片携带随机请求 ID，服务端同时校验操作人、卡片消息 ID、群 ID、任务轮次、任务仍运行、钩子心跳和有效期；按钮参数不能指定操作人。
- 用户或原任务发起人可以 allow/deny 本次具体操作，不支持会话级永久批准或扩大请求权限。
- 消息发起人能审批自己触发的具体操作，这是用户明确选择的委托规则，不等于其拥有本机所有操作权限。

## 处理流程

PermissionRequest 同步脚本 → SQLite approvals → 独立卡片发送线程 → 飞书按钮回调 → 身份及请求校验 → 决定原子记录 → 原钩子读取并返回 allow/deny。

回调不走普通任务队列，避免正在等审批的任务堵住审批决定。卡片状态区分“决定已记录”和“决定已交回审批钩子”；后者不能证明工具实际执行成功，结果仍由原任务反馈。

5 分钟到期、钩子失活、任务结束或服务重启会使待审批请求失效；无有效决定时钩子返回空 JSON，恢复 Codex 原审批流程，不自动允许。钩子进程退出后最多 5 秒心跳宽限内可能记录决定，但已死亡钩子不会消费决定或批准操作。

完整 tool_input 通过 plain_text 展示，超过 12 KB 的请求不发卡片，交给桌面查看，防止批准被截断的命令。审批卡片包含操作详情，会发到任务来源群；避免在指令参数中夹带凭据。

## 配置与信任

代码：bridge/approvals.py；模板：deploy/approval-hooks.example.json；实际配置：~/.codex/hooks.json。
本机 config.json 的 approvals.enabled=true 已启用回调处理；admin_open_ids 已登记经一次性口令和用户确认核实的管理员；群成员不能自行修改名单。

根据 OpenAI 官方 Hooks 文档，非管理钩子必须先审阅并信任。用户可在 Codex CLI 输入 /hooks，核对命令为本项目 .venv/bin/python 执行 bridge/approvals.py 后信任。禁止修改内部 trust 存储或加 bypass-hook-trust 代替用户审阅。桌面已打开的聊天是否热加载该配置，需要真实测试验证。

管理员绑定：用户在总裁办发送一次性口令，再在当前配置聊天确认发出；核对唯一消息 sender_id、来源群及时间后在本机写入管理员名单。口令不会自动赋权；如果多个账号发送、来源不明或用户未确认，保持待核实。

## 验收

1. 用户点击演示卡片，检查本机 approval_audit 确认真实回调已达（演示请求不存在，应被拒绝，不运行任务）。
2. 用户核对并信任 PermissionRequest 钩子，确认 hooks/list 返回 trusted。
3. 从总裁办发起无副作用、明确会触发权限请求的测试，观察卡片产生；本人点击批准，验证原任务继续及结果回群。
4. 再测试拒绝、非发起成员点击、过期、重复点击，以及桌面取消后卡片失效。

本地测试已覆盖发起人与管理员、无权限者、伪造按钮 actor、错群/错卡片、重复/并发决定、过期/心跳失活、任务完成、服务重启、精确轮次匹配、完整展示及钩子 JSON 输出。真实飞书回调与桌面执行尚待用户配合完成。

## 回滚

将 config.json 的 approvals.enabled 改为 false（保留其他配置），在无运行任务时重启既有 launchd 服务。删除 ~/.codex/hooks.json 中本次 PermissionRequest handler 即可撤掉钩子（如果以后有其他钩子，不能直接删整个文件）。不要删除数据库；原文字桥接继续使用。原配置备份为 var/config-before-approvals.json。

官方依据：https://learn.chatgpt.com/docs/hooks
飞书按钮：https://open.feishu.cn/document/common-capabilities/message-card/add-card-interaction/interactive-components/button?lang=zh-CN

管理员身份确认已完成，服务已重载且 running；最近 hooks/list 核实仍为 untrusted。真实远程审批尚未启用和验收。
