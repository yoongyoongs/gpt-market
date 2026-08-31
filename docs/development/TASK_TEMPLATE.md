# 开发 Task 模板

> 一个 Task 只负责一个可验证的软件工程目标。复制本模板到当前 Phase `STATUS.md` 或任务记录中使用。

## Task

- **Task ID**：`P<phase>-<sequence>`
- **Task Name**：
- **Status**：`TODO | IN_PROGRESS | DONE | PARTIAL | BLOCKED`
- **Goal**：
- **Implements**：需求编号、Baseline/详细设计章节、Acceptance ID、Guardrail ID

## Scope

### READ SCOPE

- 当前 Phase Capsule；
- 直接相关代码/测试；
- 必要的 Level 1 Contract。

### WRITE SCOPE

- 明确到目录或文件。

### Forbidden Changes

- 列出 V1/V2、其他 Phase、冻结 Contract、无关 Migration/API 等禁止项。

## Dependencies

- 前置 Task/Commit；
- 输入 DTO/Repository/API/Migration；
- 外部条件或 `UNKNOWN`。

## Acceptance

- [ ] 可验证结果一；
- [ ] 可验证结果二；
- [ ] 无 Guardrail/Contract 漂移；
- [ ] Phase STATUS 已更新；
- [ ] 中文提交并推送。

## Tests

- 自动化测试：
- 最小回归：
- 真实数据库/接口验证（如需要）：
- 文档/静态检查：

## Completion Record

- **Commit**：
- **Modified Files**：
- **Interface Change**：`NONE | 说明`
- **Migration Change**：`NONE | 说明`
- **Tests Result**：
- **Known Issues / FOLLOW_UP**：
- **Next Task**：仅标识，不自动执行。
