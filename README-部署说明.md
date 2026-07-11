# 毛泽东抗日战争思想普及智能体 Vercel 部署包

这个文件夹就是 Vercel 要关联的项目根目录。

部署时不要上传本地 `.env`。请在 Vercel 项目后台配置环境变量：

- `AI_API_KEY`：你的 DeepSeek API Key
- `AI_API_BASE`：`https://api.deepseek.com`
- `AI_MODEL`：`deepseek-v4-pro`
- `ALLOWED_ORIGINS`：可选。前后端同域部署时留空；分离部署时填写允许的 HTTPS 来源，多个来源用英文逗号分隔

访问密码已按项目要求固定为 `123456`，Vercel 中无需配置 `AGENT_PASSWORD`，已有同名变量也不会覆盖它。

安全说明：

- 真实 API Key 只能放在 Vercel 环境变量中，绝对不要写入 `.env.example`、前端代码或 GitHub
- 后端已限制请求体大小、消息长度、登录失败次数和每个 IP 的请求频率
- 限流数据保存在单个 Serverless 实例内；如需面向大量公网用户，应再接入 Vercel Firewall 或持久化限流服务
- 固定密码 `123456` 是弱密码，只适合非敏感、低额度用途

Vercel 关联项目时：

1. Root Directory 选择 `image2-vercel`
2. Framework Preset 选择 Other
3. Build Command 留空
4. Output Directory 留空
5. 部署完成后打开 Vercel 给你的网址

文件说明：

- `index.html`：前端页面
- `assets/`：头像素材
- `api/index.py`：Vercel Serverless 后端接口
- `vercel.json`：把 `/api/*` 请求转到 Python 后端
- `.env.example`：环境变量示例，不是真实密钥
