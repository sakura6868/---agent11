# 运行说明

本版默认本地离线运行。Windows运行根目录 `start.ps1`；Linux/macOS运行 `start.sh`；启动脚本创建虚拟环境、安装运行依赖并启动 http://127.0.0.1:8000 。预置测试账号为 `test / test123`，不对应真实学生身份；个人使用应注册自己的账号。

手动运行方式见 [README](../README.md)。Docker复制 `.env.example` 为 `.env` 后执行 `docker compose up --build`；数据库写入本地data目录，ZIP与新仓库不包含运行数据。

可选模型：在私人 `.env` 设置 `AGENT_LLM=1`、`AGENT_LLM_API_KEY`、`AGENT_LLM_BASE_URL`、`AGENT_LLM_MODEL`。默认关闭模型及联网搜索，不要求任何第三方凭据。密钥不写入前端、文档或Git。

生产模式使用 `APP_ENV=production`，要求独立随机 `AUTH_TOKEN_SECRET` 和 `ADMIN_API_TOKEN`，至少32字符；禁用调试管理员入口和通配跨域。生产默认禁用演示登录。公开注册和整站访问限制需要单独配置/完善，本版未包含云端上线验收。

生产数据库不得导入工程评测夹具或个人试用工作簿。雷达管理员演练入口会使用人工通知内容进入快照和事件流程；不可在真实来源监控上用它制造“官网更新”。本轮只补充文档，不宣称该入口已在生产环境禁用或完成数据隔离改造。数据类别和证据要求见 [数据来源与证据边界](DATA_PROVENANCE.md)。
