# 第三方组件说明

项目代码采用MIT许可，见 [LICENSE](../LICENSE)。依赖及版本约束见 [requirements.txt](../requirements.txt) 与 [requirements-dev.txt](../requirements-dev.txt)。FastAPI/Uvicorn提供API，Pydantic校验数据，SQLAlchemy/psycopg2访问数据库，pdfplumber/python-docx处理通知，requests处理HTTP，pytest/httpx2用于测试。第三方组件许可遵循上游声明。

前端使用本地HTML/CSS/JavaScript，不依赖CDN。LangGraph、Chroma与语义模型为可选扩展，默认使用内置编排、隔离检索与轻量向量；源码不分发模型权重。

默认未配置模型提供商或密钥，关闭外部模型及搜索。启用第三方服务时，由部署方确认模型来源、权限、计费和隐私条款。赛事公开官方文档版权归发布方，快照用于原文引用和来源复核。
