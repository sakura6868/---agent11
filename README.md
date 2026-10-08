# 校园科创导航智能体

**本仓库唯一参赛作品：校园科创导航智能体，v1.3（含开源增强）。评审请使用默认 `main` 分支根目录的完整项目。** 面向在校学生的可信赛事导航、参赛规划与执行助手。

`src/` 是本作品后端，`frontend/` 是同一应用的前端，启动根目录脚本后统一访问本地页面；`submission/` 保存本作品的提交材料，`docs/` 和 `evals/` 分别保存配套说明与评测证据。

**评审入口：** [材料总表](SUBMISSION.md) · [唯一技术文档](submission/02_技术文档.md) · [唯一正式演示视频](demo/演示视频.mp4)。当前技术文档已包含中文 BM25Plus 检索、名称纠错确认、中文数字与 JSON 容错、自动边界测试及对照结果。

评委先看 [提交入口](SUBMISSION.md)，再看 [作品说明](参赛作品说明.md)。目录为201条，其中17条关键证据完整、11条在2026-10-04快照时仍可报名；其余保留为候选或历史信息。详见 [提交快照](docs/SUBMISSION_STATUS.md)。

## 本地启动

需要 Python 3.10+。Windows 在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

打开 [本地页面](http://127.0.0.1:8000)，点击“一键体验”，或用预置测试账号 `test / test123` 登录。该账号用于功能体验，不对应真实学生身份；用户自行注册、填写并保存的画像与测试画像分开管理。

Linux/macOS 可运行 `bash start.sh`。Docker 可复制 `.env.example` 为 `.env` 后运行 `docker compose up --build`。默认使用 SQLite，关闭模型和联网搜索；不需要购买模型服务或连接云数据库。

## 体验流程

1. 在赛事大厅查看官方字段证据和来源。
2. 保存学历、技能、团队人数与每周时间画像。
3. 智能顾问选择“参赛规划”，输入“每周40小时，最多参加两场比赛”。
4. 查看六个工具的动作、观察、引用与执行清单。
5. 确认画像，在行动路线明确采用方案，再进入项目看板、日历或时间线。
6. 输入“每周0小时”，验证停止条件；未知资格和已截止赛事不会被放宽。

## 核心能力

- 官方原文、文档指纹和日期定位；未知字段保持未知，不平移官方日期。
- 证据、资格、队伍与报名状态门控，给出解释型推荐。
- 有状态的目标规划：检索、核验证据、资格检查、机会比较、组合优化、执行清单。可选模型只在允许的工具中选择下一步，最多两次；无效选择回退。
- 周容量与已有项目共同约束组合；任务、材料和截止节点联动。
- 来源变化进入人工审核，用户会话及个人资源访问受所有权校验。
- 默认使用中文分词与 BM25Plus 检索原始证据；赛事名称笔误先提示候选并要求确认。

## 验证

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe evals/run_formal_evaluation.py
.\venv\Scripts\python.exe evals/run_eval.py
.\venv\Scripts\python.exe evals/run_retrieval_comparison.py
.\venv\Scripts\python.exe scripts/check_submission_materials.py --video demo/演示视频.mp4 --require-video
```

已记录工程快照为回归182项及26子测试、正式用例15/15、模板806/806。中文检索小规模对照的 Top-1 为19/20，原字符重叠为18/20。既有浏览器检查使用离线模式；模板和检索工程对照不是独立准确率，工作量与收益是规划估计。此次材料补充没有重新运行或改写这些业务验收结果。

新增 [用户试用记录分析](docs/USER_STUDY.md) 与 [参赛效果附件](submission/06_用户试用与效果说明.md)：纳入190人、完整问卷170份、有效计时配对76组。推荐57/95、组合31/52为保留子集的总成功结果，含协助成功，并非推荐准确率或修复后提升。用户已确认实测来源；日期、版本、凭证和排除依据仍待独立核对，用户效果闭环及在线模型效果尚未完成独立验证。

## 数据来源与结果口径

赛事规则来自保存的官方资料与待核实目录；用户效果来自提供者确认来源的试用记录；工程评测使用冻结目录、开发者编写用例及合成边界夹具。三者分别统计，不能互相替代。详情见 [数据来源与证据边界](docs/DATA_PROVENANCE.md)。

队友建议当前以预置测试画像评估互补性，不提供真实可联系的学生队友库。雷达支持实际来源抓取，也保留管理员控制的变更演练入口；演练通知不是官网发布的通知，不计入真实通知同步效果。

## 交付内容

- [技术文档](submission/02_技术文档.md) · [架构](docs/ARCHITECTURE.md)
- [正式演示视频（用户指定，约3分28秒（208.05秒））](demo/演示视频.mp4)
- [正式评测](docs/QUANTITATIVE_EVALUATION.md) · [模板报告](evals/report.md)
- [用户试用报告](docs/USER_STUDY.md) · [统计附件](evals/user_study_results.json) · [试用证据索引](evidence/user_study/README.md)
- [数据来源与证据边界](docs/DATA_PROVENANCE.md) · [数据与隐私说明](docs/COMPLIANCE.md) · [第三方组件说明](docs/THIRD_PARTY_NOTICES.md)

本目录没有旧Git历史、云部署蓝图、运行数据库、私人配置或旧提交包。可选模型配置只放在本机 `.env`。生产部署需要独立强密钥、访问控制和单独验收，当前交付仅保证已记录的本地验收范围。
