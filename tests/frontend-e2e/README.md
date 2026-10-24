# 前端浏览器自动化测试

该目录使用 Playwright 在真实 Chromium 中验证 Workstation 与 Admin 的关键用户链路。
测试通过浏览器网络拦截提供固定 API 响应，不读取本地或服务器账号、数据库和密钥。

## 首次安装

```bash
npm --prefix app/workstation ci
npm --prefix tests/frontend-e2e ci
npm --prefix tests/frontend-e2e exec -- playwright install chromium
```

## 执行

```bash
npm --prefix tests/frontend-e2e test
```

Playwright 会自动启动两个 Vite 服务：

- Workstation：`http://127.0.0.1:4173`
- Admin：与工作台同一个 dev server，路径前缀 `/admin`（`http://127.0.0.1:4173/admin`）

失败时保留截图、视频和重试 Trace。HTML 报告位于
`tests/frontend-e2e/playwright-report`，执行以下命令查看：

```bash
npm --prefix tests/frontend-e2e run report
```

新增用例时按应用放入 `specs/workstation` 或 `specs/admin`，公共 API Mock 放入
`fixtures`。禁止在测试代码中写入真实账号、Token 或线上地址。

用例开头先调用 `blockUnmockedApi(page)`，结尾断言它返回的列表为空：页面新增了接口却没补 Mock
时用例直接失败并列出接口，请求不会经 Vite 代理打到本机后端。
