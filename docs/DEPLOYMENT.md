# 运行说明

本版以本地离线演示为默认交付。Windows运行根目录 `start.ps1`；Linux/macOS运行 `start.sh`；启动脚本创建虚拟环境、安装运行依赖并启动 http://127.0.0.1:8000 。虚构演示账号为 `test / test123`。

手动运行方式见 [README](../README.md)。Docker复制 `.env.example` 为 `.env` 后执行 `docker compose up --build`；数据库写入本地data目录，ZIP与新仓库不包含运行数据。

可选模型：在私人 `.env` 设置 `AGENT_LLM=1`、`AGENT_LLM_API_KEY`、`AGENT_LLM_BASE_URL`、`AGENT_LLM_MODEL`。默认关闭模型及联网搜索，不要求任何第三方凭据。密钥不写入前端、文档或Git。

生产模式使用 `APP_ENV=production`，要求独立随机 `AUTH_TOKEN_SECRET` 和 `ADMIN_API_TOKEN`，至少32字符；禁用调试管理员入口和通配跨域。生产默认禁用演示登录。公开注册和整站访问限制需要单独配置/完善，本版未包含云端上线验收。
