# 本版评测

正式用例15条，完整回归168项及26子测试，自动模板806条。结果以同目录JSON报告和 [提交快照](../docs/SUBMISSION_STATUS.md) 为准。模板由201条目录的四类问句及两条全局问句生成，不是人工标注准确率。

运行 `python evals/run_formal_evaluation.py` 和 `python evals/run_eval.py`。两者建立临时SQLite、关闭模型/外网检索，在固定日期夹具下执行，不依赖生产数据库。当前浏览器报告与视频使用离线策略；真实用户效果与在线模型表现尚未验收。
