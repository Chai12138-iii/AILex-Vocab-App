# AILex 词汇学习工具

一个本地运行的、支持 AI 多考试词汇评估的背单词工具。技术栈：Python (Flask + SQLite + Waitress) + Bootstrap 5 + DeepSeek API。

## 主要功能
- **多词书管理**：支持自定义上传词书，管理不同考试的词汇。
- **艾宾浩斯复习**：基于遗忘曲线规划复习时间，支持加练模式。
- **AI 单词重要性评估**：调用大模型，分别按六级、雅思、托福、考研标准打分并给出背诵建议。
- **纯本地运行**：所有数据（单词、进度、API Key）存在本地，无需联网（AI评估除外），保护隐私。

## 运行环境
- Python 3.10 或更高版本
- 需要访问 DeepSeek API（或通义千问兼容接口）

## 如何运行
1. 安装依赖：
   \`\`\`bash
   pip install -r requirements.txt
   \`\`\`
2. 启动程序（桌面窗口模式）：
   \`\`\`bash
   python run.py
   \`\`\`
   *或者直接运行打包好的 `AILex.exe`。*

## 配置 API Key
首次运行后，程序会在目录下自动生成 `config.txt`。请打开它，将 `sk-请替换` 替换为你自己的 DeepSeek API Key（[获取地址](https://platform.deepseek.com/api_keys)），保存后即可使用 AI 评估功能。

## 打包为 EXE
如果需要打包成独立的 exe，请执行：
\`\`\`bash
python -m PyInstaller --onefile --name AILex --icon="AILex.ico" --add-data "templates;templates" --add-data "static;static" --add-data "..\4 六级-乱序.txt;." run.py
\`\`\`

## 反馈与建议
这是我的第一个用AI做的比较完整的项目，烦请各路大佬、前辈指点批评！希望可以收到大家的意见和改进建议！