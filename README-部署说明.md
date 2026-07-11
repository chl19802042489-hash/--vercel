# 毛泽东抗日战争思想普及智能体 Vercel 部署包

这个文件夹就是 Vercel 要关联的项目根目录。

项目目录：

`C:\Users\10147\Documents\游戏生图\image2-vercel`

部署时不要上传本地 `.env`。请在 Vercel 项目后台配置环境变量：

- `AI_API_KEY`：你的 DeepSeek API Key
- `AI_API_BASE`：`https://api.deepseek.com`
- `AI_MODEL`：`deepseek-v4-pro`
- `AGENT_PASSWORD`：访问密码，默认可设为 `123456`

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
