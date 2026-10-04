# 校园科创导航智能体

v1.3 纯净参赛版。面向在校学生的可信赛事导航、参赛规划与执行助手。

评委先看 [提交入口](SUBMISSION.md)，再看 [作品说明](参赛作品说明.md)。目录为201条，其中17条关键证据完整、11条在2026-10-04快照时仍可报名；其余保留为候选或历史信息。详见 [提交快照](docs/SUBMISSION_STATUS.md)。

## 本地启动

需要 Python 3.10+。Windows 在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .start.ps1
```

打开 [本地页面](http://127.0.0.1:8000)，点击“一键体验”，或用虚构账号 `test / test123` 登录。

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

## 验证

```powershell
.envScriptspython.exe -m pip install -r requirements-dev.txt
.envScriptspython.exe evals/run_formal_evaluation.py
.envScriptspython.exe evals/run_eval.py
.envScriptspython.exe scripts/check_submission_materials.py --video demo/演示视频.mp4 --require-video
```

本版回归168项及26子测试、正式用例15/15、模板806/806。浏览器检查和视频使用离线模式；模板回归不是独立人工标注准确率，工作量与收益是规划估计，真实学生效果和在线模型效果尚未验证。

## 交付内容

- [技术文档](submission/02_技术文档.md) · [架构](docs/ARCHITECTURE.md)
- [3分40秒实际浏览器演示](demo/演示视频.mp4) · [旁白稿](docs/DEMO_NARRATION.md)
- [正式评测](docs/QUANTITATIVE_EVALUATION.md) · [模板报告](evals/report.md)
- [数据与隐私说明](docs/COMPLIANCE.md) · [第三方组件说明](docs/THIRD_PARTY_NOTICES.md)

本目录没有旧Git历史、云部署蓝图、运行数据库、私人配置或旧提交包。可选模型配置只放在本机 `.env`。生产部署需要独立强密钥、访问控制和单独验收，当前交付仅保证已记录的本地验收范围。
