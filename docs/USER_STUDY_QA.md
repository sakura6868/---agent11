# 用户试用材料核验记录

材料修复日期：2026-10-10。范围为公开聚合数、筛选审计、校验脚本、文件链接和SHA-256；不核验原始学生操作真实性，不重跑原业务验收。

## 修复来源

远端提交 `25f55b8` 包含用户研究文档，但缺少被引用的统计JSON、筛选审计、复算脚本、校验器、单元测试和指纹清单。工作区及远端分支没有找到源工作簿或这些文件，不能保留“已经重算工作簿”和“原文件指纹已确认”的陈述。

本轮从现有公开报告转录聚合计数，明确标注 `team_reported_aggregate_unverified`。用户于2026-10-09确认参与者全部来自计算机学院；学院归属记录为团队声明，未独立核验。

## 实际检查

| 检查 | 结果与范围 |
| --- | --- |
| 聚合算术 | 十个任务的独立/协助成功、未成功与分母一致；总尝试为1,235 |
| 筛选一致性 | 1,913减136等于1,777；移除分类计数一致，逐条排除理由仍未提供 |
| 筛选敏感性 | T04基线36.8%、保留60.0%；T05基线24.2%、保留59.6%，成功数不变 |
| 单元测试 | `python -m unittest scripts.test_user_study_evidence -v`：14项通过 |
| 仓库内链接 | `python scripts/check_user_study_materials.py`：检查README及docs/submission/evidence中的相对链接 |
| 提交快照 | 自动读取聚合数、记录其SHA-256；证据不完整时保持 `real_user_validation=false` |
| 文件指纹 | 清单覆盖10个公开文本，统一LF换行后计算SHA-256；兼容Windows/Linux检出，清单自身不自引用 |
| 材料与视频 | `python scripts/check_submission_materials.py --video demo/演示视频.mp4 --require-video`：22项材料，视频208.05秒 |

14项单元测试覆盖聚合一致性、错误比例、协助成功漏记、负数、零分母、重复任务、推荐分母、计时不一致、筛选不一致、缺指纹、缺报告、错误验证声明、文件被修改和根目录/嵌套链接。测试验证校验器，不新增学生样本。

## 复现顺序

```bash
python scripts/analyze_user_study.py
python scripts/refresh_submission.py
python scripts/check_user_study_materials.py --write-manifest --document-date 2026-10-10
python scripts/check_user_study_materials.py
python -m unittest scripts.test_user_study_evidence -v
python scripts/check_submission_materials.py --video demo/演示视频.mp4 --require-video
```

第一次运行可在缺清单时生成；以后仅在审核过真实变更后更新清单。指纹证明文件一致性，不证明研究真实性。源工作簿指纹为null，实际试用日期、版本绑定、原始操作凭证、授权和逐条排除理由仍待采集者提供。原工程报告保留原日期及结果。
