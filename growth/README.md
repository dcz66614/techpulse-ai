# growth/ — G10 合规样报与访谈准备

本目录仅包含 G10-prep 的草稿与模板，不含真实样报、真实客户信息或对外营销材料。

## 文件

| 文件 | 状态 | 说明 |
|---|---|---|
| sample_template.md | 模板 | 证据完整样报的骨架；所有尖括号均为占位符 |
| sample_1.md | 生成 | 7 日窗口样报。发布状态以文件内 q10_status 为准 |
| sample_2.md | 生成 | 28 日窗口样报。发布状态以文件内 q10_status 为准 |
| interview_guide.md | 草稿 | 可选客户访谈脚本、知情同意话术、匿名反馈表与 CRM 字段 |
| README.md | 说明 | 本文件 |

## sample_1.md 与 sample_2.md

- `sample_1.md` 与 `sample_2.md` 由 `scripts/build_samples.py` 写入。
- 脚本读取 `tests/fixtures/evidence/records.jsonl`，依次运行去重、特征、评分、实体、证据包、反证、R10 草稿、R20 渲染和 Q10。
- 样报里的事实、数字和链接只复制这些输出，不手写。
- `q10_status` 为 pass 且 `qa_pass` 为 true 时，样报标注 publishable。
- 其余样报标注 internal demo only，不得对外展示、发送或引用。
- 每份样报标注“AI 生成”，并带上流水线给出的 Q10 结果。
- 重新生成：`uv run python scripts/build_samples.py`

## 对外边界

- 本目录内容均为内部草稿，不自动联系第三方。
- 不购买名单、不群发、不批量私信。
- 发送任何访谈邀请、问卷、样报或营销材料前，必须取得 AUTH-06 用户可追溯批准。
- 访谈与 CRM 记录不得包含姓名、邮箱地址、电话号码或其他个人联系方式，也不得用于模型训练。

## 验证

运行：

```text
python -m pytest -q tests/growth
```

测试检查准备文件存在、UTF-8 编码、AUTH-06 提示、模板必需字段，以及这些准备文件不包含真实 http(s) 链接。`tests/growth/test_samples.py` 检查两份样报可重复生成、带有 Q10 状态、链接都出现在 fixtures 中，且没有个人联系方式。
