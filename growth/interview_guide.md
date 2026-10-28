# TechPulse-AI 客户访谈指南（草稿）

> 文档状态：G10-prep 内部草稿。仅用于内部准备，不是对外营销材料。
> 起草前置条件：R10 报告草稿与 Q10 事实 QA 尚未完成，真实样报尚不存在。
> 边界：本指南不自动联系任何第三方；不得购买名单、群发或批量私信。向任何外部对象发送访谈邀请、问卷或预约信息前，必须先取得 AUTH-06 用户可追溯批准。

## 1. 目标与范围

本访谈用于验证以下假设，而不是推销产品：

1. 目标角色目前如何完成技术趋势跟踪与研究。
2. 他们今天使用哪些替代方案，成本与缺口是什么。
3. 是否存在可核对的实际预算、采购责任与试点意愿。
4. $99 / $199 / $299 的月价锚点只是实验假设，不是市场成交价；访谈只记录反应，不诱导成交。

优先角色（ICP 假设，来自项目书第 3.1 节）：

- AI 软件团队 CTO / Tech Lead
- 企业创新或战略团队成员
- 研发工程师 / 架构师
- 科技分析师或行业媒体研究者

## 2. 访谈前准备

- 仅在已获 AUTH-06 批准的渠道与名单范围内发出邀请。
- 访谈为自愿、可拒绝、可随时退出。
- 默认不录音；如参与者明确同意，录音仅用于内部转写与结构化编码，转写完成后按 <RETENTION_DAYS> 删除原始音频。
- 只收集匿名化字段；不收集姓名、邮箱地址、电话号码、社交账号或其他个人联系方式。
- 不把访谈内容用于模型训练。
- 访谈记录仅限内部研究使用，不对外展示。

## 3. 知情同意话术

向参与者说明并获得明确同意后，才开始提问。建议逐字使用以下话术，并记录同意状态。

> 这是一次可选的内部产品研究访谈，预计 <INTERVIEW_DURATION>。您可以拒绝回答任何问题，也可以随时退出。我们不会记录您的姓名、邮箱地址、电话号码或社交账号。默认不录音；如果您同意录音，录音只用于内部转写和匿名化编码，原始音频会在 <RETENTION_DAYS> 内删除。访谈内容不会用于模型训练，只会以匿名汇总形式用于产品研究。是否同意参与？是否同意录音？

记录字段：

- consent_version: <CONSENT_VERSION>
- consent_date: <DATE>
- consent_status: <GRANTED_DECLINED_WITHDRAWN>
- recording_consent: <YES_NO>
- withdrawal_requested: <TRUE_FALSE>

## 4. 访谈脚本

### 4.1 暖场与背景（不涉及个人身份）

1. 您目前所在的团队大致负责哪类产品或技术工作？
2. 您的角色更偏向技术决策、研发执行、战略研究还是内容分析？
3. 团队规模大致在哪个区间？（只记录区间，不记录公司名）

### 4.2 当前工作流

1. 请回忆最近一次需要判断某项技术是否值得跟进的情形：您先做了什么，然后做了什么？
2. 您通常从哪里获得信息？请按实际使用频率排序。
3. 从看到信号到形成结论，通常需要多长时间？最耗时的步骤是什么？
4. 哪些环节最容易遗漏反证、来源冲突或时间窗口？
5. 您如何保存、复用或向同事解释这些结论？

### 4.3 替代方案与缺口

1. 目前使用哪些工具或服务替代这类工作？请区分免费、付费和内部自建。
2. 这些方案最让您不满意的地方是什么？请举一个最近发生的具体例子。
3. 如果有一个持续更新的公开证据报告，您最希望它解决哪一个具体问题？
4. 哪些内容您不会信任，或不会用于对外材料？

### 4.4 预算与付费意愿

1. 这类信息工作目前是否有独立预算？预算由谁持有或审批？
2. 过去 <PURCHASE_LOOKBACK_WINDOW> 内是否为类似工具或研究服务付费？请描述采购流程，而不是猜测金额。
3. 如果只能选一种计费方式，您更倾向按席位、按主题、按 API 用量还是按更新频率？
4. 对以下实验月价锚点，您的第一反应分别是什么：$99、$199、$299？
5. 在什么条件下您会愿意进入试用？试用后什么结果会让您愿意付费？
6. 请区分：口头兴趣、试用意向、已批准的预算、真实已支付。您目前处于哪一阶段？

### 4.5 试点与决策

1. 如果要评估一个试点，谁会参与决策？需要哪些材料？
2. 试点成功的可观察标准是什么？
3. 有哪些合规、采购或数据使用限制需要提前知道？
4. 是否愿意在匿名条件下接收一份样报用于评估？如愿意，后续邀请仍需 AUTH-06 批准并由内部流程发出。

## 5. 访谈后匿名反馈表字段

以下字段进入匿名反馈表，不包含个人联系方式。

- feedback_id: <FEEDBACK_ID>
- role: <ROLE>
- company_size_band: <SIZE_BAND>
- industry: <INDUSTRY>
- region_band: <REGION_BAND>
- current_tools: <TOOL_CATEGORY>
- current_alternatives: <ALTERNATIVES>
- workflow_pain_points: <ANON_CODED>
- time_cost_band: <TIME_BAND>
- budget_band: <BUDGET_BAND>
- budget_owner: <YES_NO_UNSURE>
- purchase_process: <ANON_CODED>
- price_anchor_reaction: <REACTION_CODED>
- willingness_to_pay_stage: <NONE_VERBAL_TRIAL_BUDGET_PAID>
- pilot_interest: <NONE_VERBAL_TRIAL_PAID>
- decision_criteria: <ANON_CODED>
- consent_version: <CONSENT_VERSION>
- consent_status: <GRANTED_DECLINED_WITHDRAWN>
- recording_consent: <YES_NO>
- interview_date: <DATE>
- interviewer_id: <INTERNAL_ID>
- do_not_contact: <TRUE_FALSE>
- notes_anonymised: <ANON_NOTES>

## 6. CRM 字段清单

CRM 只保存匿名或内部标识字段。禁止写入姓名、邮箱地址、电话号码、社交账号、公司可识别名称或任何个人联系方式。

| 字段 | 类型 | 说明 |
|---|---|---|
| crm_record_id | 内部 ID | 系统生成的匿名记录标识 |
| account_pseudonym | 内部 ID | 公司或机构的匿名代号 |
| role | 枚举 | 角色类别，不含姓名 |
| company_size_band | 枚举 | 规模区间 |
| industry | 枚举 | 行业类别 |
| region_band | 枚举 | 粗粒度区域 |
| source_channel_category | 枚举 | 渠道类别，不记录个人联系方式 |
| consent_status | 枚举 | 同意状态 |
| auth06_approval_id | 内部 ID | 对外邀请所依据的 AUTH-06 批准记录 |
| interview_status | 枚举 | 未开始 / 已完成 / 拒绝 / 退出 |
| budget_band | 枚举 | 预算区间 |
| budget_owner | 枚举 | 是否有预算持有人 |
| purchase_authority | 枚举 | 采购权限：有 / 无 / 不确定 |
| price_anchor_reaction | 枚举 | 对实验价格锚点的编码反应 |
| pilot_stage | 枚举 | 无 / 口头兴趣 / 试用 / 已付费 |
| next_step_internal | 文本 | 仅内部下一步，不含个人联系方式 |
| do_not_contact | 布尔 | 参与者要求不再联系时置为真 |
| updated_at | 时间 | 记录更新时间 |

## 7. 禁止事项与数据处理

- 不自动联系第三方；不发送冷邮件、批量私信或购买名单。
- 不记录个人联系方式、姓名、可识别公司信息或原始录音以外的身份线索。
- 不在公开渠道引用可识别个人的原话。
- 不把访谈记录、转写文本或个人数据用于模型训练。
- 访谈原始记录按 <RETENTION_DAYS> 保存，到期删除；退出请求立即生效。
- 对外发送任何邀请或营销材料前，必须取得 AUTH-06 批准。

## 8. 编码与汇总

- 访谈结束后 <CODING_DEADLINE> 内完成匿名编码。
- 只汇总区间、类别和编码后的反应，不汇总可识别个人的信息。
- 报告中区分“口头意向”“试用”“真实已支付”，不得把意向写成成交。
- 样本不足时输出 insufficient_sample，不编造客户、订单或预算数字。
