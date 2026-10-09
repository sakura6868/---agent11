# 本版评测

正式用例15条，已记录完整回归182项及26子测试，自动模板806条。结果以同目录JSON报告和 [提交快照](../docs/SUBMISSION_STATUS.md) 为准。模板由201条目录的四类问句及两条全局问句生成，不是人工标注准确率。

运行 `python evals/run_formal_evaluation.py` 和 `python evals/run_eval.py`。两者建立临时SQLite、关闭模型/外网检索，在固定日期夹具下执行，不依赖生产数据库。当前浏览器报告与视频使用离线策略；真实用户效果与在线模型表现尚未验收。

正式评测包含人工构造的合成赛事与来源片段，用于验证截止、资格及缺证据等边界；它们不是官方通知。工程报告保留原有结果，不改称学生实测，也不将模板通过率当作实际使用准确率。统一分类见 [数据来源与证据边界](../docs/DATA_PROVENANCE.md)。

新增用户记录结果为 [user_study_results.json](user_study_results.json)，方法见 [用户试用报告](../docs/USER_STUDY.md)。仅公开聚合统计与指纹，不包含个人行。已有用户提供记录分析，独立效果凭证尚待闭环；不把学生成功率、自动测试通过率与检索Top-1混用。

当前聚合JSON由现有公开报告转录，原始工作簿未提供。用户确认全部参与者来自计算机学院；身份仍未独立核验。运行 `python scripts/analyze_user_study.py` 可重算百分比与筛选敏感性，`python scripts/check_user_study_materials.py` 校验文件、链接、快照和指纹。均分和配对耗时保存报告值，缺少原始行时无法复算这些统计量。
