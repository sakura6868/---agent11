# 第三方组件说明

项目代码采用MIT许可，见 [LICENSE](../LICENSE)。依赖及版本约束见 [requirements.txt](../requirements.txt) 与 [requirements-dev.txt](../requirements-dev.txt)。FastAPI/Uvicorn提供API，Pydantic校验数据，SQLAlchemy/psycopg2访问数据库，pdfplumber/python-docx处理通知，requests处理HTTP，pytest/httpx2用于测试。`cn2an` 及其文本预处理依赖 `proces`（均为MIT许可，<https://github.com/Ailln/cn2an>、<https://github.com/Ailln/proces>）仅用于把“十小时、两场、三人”等中文数字转为结构化约束；转换结果仍由本项目的范围校验、资格门控和组合优化复核。`json-repair`（MIT，<https://github.com/mangiucugna/json_repair>）仅修复模型工具选择结果的轻微JSON语法错误，修复后仍必须通过单字段结构、工具白名单和参数校验。第三方组件许可遵循上游声明。

前端使用本地HTML/CSS/JavaScript，不依赖CDN。LangGraph、Chroma与语义模型为可选扩展，默认使用内置编排、隔离检索与轻量向量；源码不分发模型权重。

本次接入的开源增强：

| 组件 | 版本 | 上游与许可证 | 实际用途 |
| --- | --- | --- | --- |
| jieba | 0.42.1 | [fxsjy/jieba](https://github.com/fxsjy/jieba)，MIT | 默认离线检索的中文搜索分词 |
| rank-bm25 | 0.2.2 | [dorianbrown/rank_bm25](https://github.com/dorianbrown/rank_bm25)，Apache-2.0 | 同赛事原文证据的 BM25Plus 排序，依赖 NumPy（BSD-3-Clause） |
| RapidFuzz | 3.14.3 | [rapidfuzz/RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)，MIT | 近似名称候选；不自动锁定赛事或年份 |
| Hypothesis | 6.168.3 | [HypothesisWorks/hypothesis](https://github.com/HypothesisWorks/hypothesis/blob/master/LICENSE.txt)，MPL-2.0 | 开发测试中的自动输入生成及失败案例缩减，依赖 sortedcontainers（Apache-2.0） |

以上组件通过包管理器安装，未复制或修改其源码，源码归档不包含第三方安装目录；分发安装后环境时应保留对应许可证与版权声明。BM25、编辑距离和自动输入生成不是本项目原创算法。Hypothesis 仅列入开发依赖，不影响评委启动应用。

默认未配置模型提供商或密钥，关闭外部模型及搜索。启用第三方服务时，由部署方确认模型来源、权限、计费和隐私条款。赛事公开官方文档版权归发布方，快照用于原文引用和来源复核。
