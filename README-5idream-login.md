# 到梦空间扫码登录工具

## 环境

- Windows
- Python 3.10 或更高版本
- 可访问 `https://www.5idream.net/`

## 安装依赖

```powershell
python -m pip install -r requirements-5idream-login.txt
```

## 运行

```powershell
python 5idream_login.py
```

脚本会生成二维码，扫码确认后保存两个整理结果：

- `5idream-report.md`：可读的 Markdown 报告
- `5idream-data.json`：整理后的 JSON 数据

登录成功后可以在控制台选择活动分类、部落分类，并按编号查看活动详情。所有网络请求前随机等待 1 到 2 秒。

## 自定义输出路径

```powershell
python 5idream_login.py --report-out report.md --json-out data.json
```

请只在本人有权使用的账号和环境中运行，不要分享二维码、Token、Cookie 或个人数据文件。
