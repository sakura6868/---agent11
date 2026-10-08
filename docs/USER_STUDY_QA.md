# 用户试用材料核验记录

核验范围：新增聚合统计、文档、链接、快照、筛选分类和公开范围。不重新运行业务验收，不核验原始学生操作真实性。

文档补充日期2026-10-08，统计冻结日期2026-10-07。

## 已执行结果

`python scripts/check_user_study_materials.py`：12/12项材料检查通过。

| ID | 检查 | 结果 |
| --- | --- | --- |
| US-QA-01 | 聚合数量、成功率、配对耗时和筛选算术一致 | 通过 |
| US-QA-02 | 独立筛选附件与统计JSON内嵌审计一致 | 通过 |
| US-QA-03 | 提交快照、统计指纹及效果验证状态一致 | 通过 |
| US-QA-04 | 待补原始证据未被标成已独立验证 | 通过 |
| US-QA-05 | 详细报告十个任务的分子分母与成功类型一致 | 通过 |
| US-QA-06 | 参赛附件七个任务行与详细报告一致 | 通过 |
| US-QA-07 | 八项问卷均分与高分人数一致 | 通过 |
| US-QA-08 | 配对耗时表与统计JSON一致，保留子集范围 | 通过 |
| US-QA-09 | 十六份相关文档的仓库内链接可解析 | 通过 |
| US-QA-10 | 六项证据索引文件存在且指纹一致 | 通过 |
| US-QA-11 | 公开聚合未包含参与者行ID、邮箱或源工作簿 | 通过 |
| US-QA-12 | 七份原工程结果文件与基础提交逐字节一致 | 通过 |

`python -m unittest scripts.test_user_study_evidence -v`：14/14项测试通过，覆盖正确报表、错误比例、协助成功漏记、负数、零分母、重复任务、推荐分母不一致、计时不一致、筛选不一致、缺指纹、缺报告，以及有数据但凭证未闭环的状态。测试只检查校验器，不是新增学生样本。

`python scripts/check_submission_materials.py --video demo/演示视频.mp4 --require-video`：22项必需材料齐全；原正式视频208.05秒，符合3—5分钟要求。本轮未更换或重录视频。

`git diff --check`：通过。复算前后源工作簿指纹一致，未修改源文件。原工程结果包括正式结果、模板结果、模板报告、检索对照、浏览器结果、发布验收及安全扫描，均未改写；本轮未重跑业务验收。

## 可重复检查

```bash
python scripts/refresh_submission.py
python scripts/check_user_study_materials.py --write-manifest --document-date 2026-10-08
python -m unittest scripts.test_user_study_evidence -v
python scripts/check_submission_materials.py --video demo/演示视频.mp4 --require-video
```

重新生成指纹前须先审核文档和数据的实际变更。指纹更新不补齐原始凭证，不自动认可新的真实性、授权或效果结论。

原始凭证、实际日期、被试用版本、排除理由及授权复核待补，不能因材料校验通过就宣称独立用户效果验证已完成。
