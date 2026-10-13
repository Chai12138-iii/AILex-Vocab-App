"""
六级背单词网页应用 - 第三版（AI词汇评估系统）
技术栈：Python + Flask + sqlite3（只需 pip install flask requests，其他全用自带库）

【重构说明】本次只做代码结构优化，业务逻辑和数据库结构完全不变：
  1. 所有可调参数（API密钥 / 路径 / 学习规则 / AI参数等）集中到文件最前面的
     "全局配置区"，改配置只需要动这一块；
  2. 数据库连接统一交给 open_db() 上下文管理器：离开 with 块（包括中途 return
     或报错）自动关闭连接，各路由不再手写 conn = get_db_connection() / conn.close()；
  3. 抽取重复代码：待复习数量统计 SQL（原来 4 处逐字重复）、首页/复习页的
     公共页面数据、提交路由里两段完全相同的 UPDATE 语句；
  4. 清理死代码：REVIEW_INTERVALS 里永不生效的 "6: 30" 备用项、
     reset_book / continue_learning 中从未使用的变量等。

数据库结构零改动：旧的 cet6.db 可以直接沿用，学习数据不受影响。
"""

import os
import sys         # 【打包适配】用 sys.frozen / sys.executable / sys._MEIPASS 判断运行环境
import re          # 用正则清理 AI 返回内容里的 ```json 代码块标记
import json        # 解析 AI 返回的 JSON 字符串
import sqlite3
from contextlib import contextmanager   # 【重构新增】实现 open_db() 上下文管理器
from datetime import datetime, timedelta

import requests    # 调用大模型 API（需 pip install requests）
import urllib3     # 用于关闭 verify=False 时刷屏的 InsecureRequestWarning 警告
from flask import Flask, render_template, request, redirect, url_for, flash, session

# ====================================================================
# 屏蔽 HTTPS 不安全请求警告
# --------------------------------------------------------------------
# 调用大模型 API 时必须带 verify=False（不校验 SSL 证书），否则本机杀毒软件
# （如卡巴斯基）的 HTTPS 抓包代理会让 requests 校验证书失败、直接报错。
# 但 verify=False 会让 urllib3 每次请求都打印一条红色警告，这里全局关掉它。
# ====================================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ====================================================================
# 【核心重构①】全局配置区 —— 所有可调参数集中在这里，要改什么只动这一块
# ====================================================================

# --------------------------------------------------------------------
# 1. 路径配置
# --------------------------------------------------------------------
# 【打包适配·核心】区分"数据目录"与"资源目录"两个概念：
#
#   开发环境（getattr(sys, 'frozen', False) 为 False）：
#     两者都是本文件所在目录，与旧版行为完全一致，直接 python app.py 照常跑。
#
#   PyInstaller 打包后（sys.frozen 为 True）：
#     - 数据目录 DATA_DIR = EXE 所在的真实目录。
#       cet6.db / config.txt / settings.txt 等需要长期读写的文件放这里，
#       用户关掉程序数据还在。绝不能用 __file__（onefile 模式下它指向
#       临时解压目录 _MEIPASS，程序一关目录就被删，数据等于"失忆"）。
#     - 资源目录 RESOURCE_DIR = sys._MEIPASS（PyInstaller 的临时解压目录）。
#       templates / static / 默认词库 txt 这些只读资源由打包命令
#       --add-data 塞进包里，运行时从这里读。
# --------------------------------------------------------------------
FROZEN = getattr(sys, 'frozen', False)          # True = 打包后的 EXE，False = 开发环境
if FROZEN:
    DATA_DIR = os.path.dirname(sys.executable)  # EXE 所在的真实目录（数据写这里）
    RESOURCE_DIR = getattr(sys, '_MEIPASS', DATA_DIR)  # 临时解压目录（只读资源在这里）
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = DATA_DIR

DB_PATH = os.path.join(DATA_DIR, 'vocab.db')                       # SQLite 数据库文件（数据）
# 【平滑迁移】旧版数据库文件名：init_database() 启动时若发现 DATA_DIR 下
# 存在旧的 cet6.db 且还没有 vocab.db，会自动改名升级（见 init_database 开头）
OLD_DB_PATH = os.path.join(DATA_DIR, 'cet6.db')
# 默认词库源文件（只读资源，仅首次建库导入单词时使用）：
#   开发环境：沿用旧逻辑，放在上级目录（danci 根目录）下；
#   打包后：从资源目录 _MEIPASS 读取，打包时需用 --add-data 把它带进包里
if FROZEN:
    DATA_FILE = os.path.join(RESOURCE_DIR, '4 六级-乱序.txt')
else:
    DATA_FILE = os.path.join(os.path.dirname(DATA_DIR), '4 六级-乱序.txt')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.txt')             # 每日目标设置（数据）
DAILY_PROGRESS_FILE = os.path.join(DATA_DIR, 'daily_progress.txt') # 今日新词进度（数据）
# 【新增】API Key 配置文件：密钥不再硬编码在代码里，改从这个文件读取
CONFIG_FILE = os.path.join(DATA_DIR, 'config.txt')                 # API 密钥（数据）
# 当前选中的词书 ID 文件（内容就是一个数字，如 "1"）
# 单独用文件而不是存数据库，和 settings.txt/daily_progress.txt 保持同一套思路
CURRENT_BOOK_FILE = os.path.join(DATA_DIR, 'current_book.txt')     # 当前词书（数据）

# Flask 的模板 / 静态文件目录（只读资源），在下方 Flask(...) 构造时显式指定，
# 这样开发和打包环境都用同一套显式路径，不依赖 __file__ 的相对位置推断
TEMPLATE_DIR = os.path.join(RESOURCE_DIR, 'templates')
STATIC_DIR = os.path.join(RESOURCE_DIR, 'static')

# --------------------------------------------------------------------
# 2. 学习规则配置（艾宾浩斯遗忘曲线）
# --------------------------------------------------------------------
# key = 点击"认识"后到达的新阶段，value = 距下次复习的天数
# 【死代码清理】原版这里还有一项 6: 30（注释"备用"）：submit 路由用
# REVIEW_INTERVALS.get(new_stage, 30) 取间隔，缺省值本来就是 30，
# 所以这一项无论在不在，结果都完全一样，属于永远不会生效的冗余配置，已删除。
REVIEW_INTERVALS = {
    1: 1,     # 第1次认识 -> 1天后再复习
    2: 2,     # 第2次认识 -> 2天后再复习
    3: 4,     # 第3次认识 -> 4天后再复习
    4: 7,     # 第4次认识 -> 7天后再复习
    5: 15,    # 第5次认识 -> 15天后再复习（到达此阶段标记为已掌握）
}
MAX_STAGE = 5                 # 复习阶段上限，到达即标记为"已掌握"
DEFAULT_DAILY_GOAL = 20       # 每日新词目标默认值（可在页面设置里修改）

# --------------------------------------------------------------------
# 3. AI 评估配置（大模型 API）
# --------------------------------------------------------------------
# 【重要修改】API_KEY 不再硬编码在代码里，改从 config.txt 文件读取！
#   - 好处1：密钥不会跟着代码泄露（比如截图分享代码、上传 Git 仓库）
#   - 好处2：换密钥只需改 config.txt 一个文件，不用动代码
#   DeepSeek 获取地址：https://platform.deepseek.com/api_keys
#   通义千问（阿里云百炼）也兼容这套写法，只需同时改 API_URL 和 MODEL_NAME：
#     API_URL    = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
#     MODEL_NAME = "qwen-plus"
API_KEY_PLACEHOLDER = 'sk-请替换'   # config.txt 的默认占位符


def load_api_key():
    """
    【新增】从 config.txt 读取 API Key。

    流程：
    1. 如果 config.txt 不存在（第一次运行新版代码），自动创建它，
       并写入默认占位符 'sk-请替换'，提醒用户去填真实密钥；
    2. 读取文件内容，strip() 去掉首尾空格/换行（防止复制密钥时多带了空格）；
    3. 读取出错（如权限问题）返回占位符，让程序不至于崩溃，
       后续 AI 评估时会给出明确的中文提示。
    """
    if not os.path.exists(CONFIG_FILE):
        # 首次运行：自动创建配置文件，写入占位符
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                f.write(API_KEY_PLACEHOLDER)
        except OSError:
            pass   # 创建失败也不影响启动，后面读取会再走 except 分支
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return API_KEY_PLACEHOLDER


def is_api_key_valid():
    """
    【新增】判断当前 API_KEY 是否可用（非空且不是默认占位符）。
    AI 评估路由在调用大模型前先检查这里，密钥未配置时
    直接给出中文提示，不再发出一个注定 401 的请求。
    """
    return bool(API_KEY) and API_KEY != API_KEY_PLACEHOLDER


# 启动时从 config.txt 读取一次，整个运行期间使用
API_KEY = load_api_key()

# 大模型对话接口地址（OpenAI 兼容格式，DeepSeek / 通义千问等都适用）
API_URL = "https://api.deepseek.com/chat/completions"

# 模型名称：deepseek-chat 是 DeepSeek 的通用对话模型
MODEL_NAME = "deepseek-flash"

# 评分任务要稳定、可复现，温度调低一些（0.3），减少模型随意发挥
AI_TEMPERATURE = 0.3
# AI 请求超时秒数：超时抛异常，避免页面一直转圈卡死
AI_TIMEOUT = 30

# 【核心重构①】System Prompt 模板：原来是一个独立函数 build_ai_system_prompt()，
# 现在把 Prompt 内容本身放进配置区，用 {exam_type} 作占位符，
# 调用时 .format(exam_type=...) 即可，生成的文案与原来的 f-string 完全一致。
# 例如 exam_type='雅思' 时，AI 收到的人设就是"资深英语雅思考试专家"，
# 评估依据也变成"历年雅思真题"，同一个词在不同考试下可能得到不同分数。
AI_SYSTEM_PROMPT_TEMPLATE = (
    "你是资深英语{exam_type}考试专家。"
    "请评估用户给出的单词在历年{exam_type}真题中的重要性。"
    "请严格输出 JSON 格式，包含字段："
    "meaning（中文释义）, score（1-5的整数）, reason（评估理由，30字以内）。"
    "不要输出任何其他文字。"
)

# 支持的考试类型白名单（代码层枚举约束）：
#   - 后端只接受这个列表里的 exam_type，防止前端手动构造非法值写入数据库
#   - 前端的评估按钮、卡片标签都根据这份清单生成
# 想增加新考试（如"专四"）时，只需要在这里加一个名字，前后端会自动支持
EXAM_TYPES = ['六级', '雅思', '托福', '考研']

# 默认考试类型：老数据（exam_type 字段升级前评估的单词）统一归为"六级"
DEFAULT_EXAM_TYPE = '六级'

# 【改造】AI 评估词库按考试类型分库存储：每个考试类别一本专属词书，
# 不再把所有类别的单词混进同一本"AI评估词库"。
# 键 = exam_type（表单/URL 里传的类型名），值 = books 表里对应的词书名。
# init_database() 启动时会自动补齐缺失的分类词书（见下方）。
AI_BOOK_NAMES = {
    '六级': 'AI评估词库-六级',
    '雅思': 'AI评估词库-雅思',
    '托福': 'AI评估词库-托福',
    '考研': 'AI评估词库-考研',
}

# --------------------------------------------------------------------
# 4. Flask 配置
# --------------------------------------------------------------------
# flash 消息（上传成功/失败的提示）依赖 session 签名，必须设置 secret_key。
# 这里是本地学习小工具，用一个固定字符串即可，Flask 自带该功能无需装任何包。
SECRET_KEY = 'danci-word-app-secret'

# 【打包适配】显式指定 templates / static 目录：
# 开发环境它们就是项目目录（行为与旧版一致）；打包后 RESOURCE_DIR 指向
# _MEIPASS 临时解压目录，Flask 按这里指定的路径去找模板和静态文件
app = Flask(__name__,
            template_folder=TEMPLATE_DIR,
            static_folder=STATIC_DIR)
app.secret_key = SECRET_KEY


# ====================================================================
# 【核心重构②】数据库连接管理
# ====================================================================

def get_db_connection():
    """
    创建一个数据库连接（open_db 内部和初始化流程都会用到）。

    【并发优化】两处增强，专门解决 SQLite 的 "database is locked" 报错：
    1. timeout=15：数据库被其他连接占用时，最多等待 15 秒再放弃，
       而不是默认的立刻报错。快速连点、多标签页同时操作时有充足缓冲。
    2. PRAGMA journal_mode=WAL：开启 WAL（预写日志）模式，
       允许"读操作不阻塞写操作、写操作不阻塞读操作"，
       比默认的 rollback journal 模式并发性能好很多。
       （WAL 是数据库级持久设置，设置一次永久生效，
       这里每条连接都执行是幂等操作，无副作用。）
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def open_db():
    """
    【核心重构②】数据库连接上下文管理器 —— 所有路由统一用它管理连接。

    用法：
        with open_db() as conn:
            conn.execute(...)

    离开 with 块时（正常结束、中途 return、抛异常三种情况都包括）
    连接都会自动关闭，各路由不再需要手写 conn.close()，
    也不会再出现"某个错误分支忘记关连接"的隐患。

    注意：这里只负责"自动关闭"，不自动 commit ——
    提交时机仍由各处显式调用 conn.commit() 控制，保证业务行为与重构前完全一致。
    """
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


# ====================================================================
# 数据库初始化
# ====================================================================

def init_database():
    """初始化数据库：建表、升级旧表、导入数据（逻辑与重构前完全一致）"""
    # ---- 【平滑迁移】旧库文件改名：cet6.db -> vocab.db ----
    # 条件：DATA_DIR 下存在旧的 cet6.db，且 vocab.db 不存在。
    # 健壮性增强：vocab.db 若存在但"一张表都没有"，说明它是被
    # sqlite3.connect() 顺手创建的空占位文件（比如其他程序先碰过它），
    # 这种空壳也要删掉再迁移，否则老用户数据会被永远挡在门外。
    # 把主库连同 WAL 模式的两个衍生文件（-wal / -shm）一起改名，
    # 这样未 checkpoint 的数据也能完整跟随，老用户数据一个不丢。
    # 放在 init_database() 最前面：所有启动入口（python app.py / run.py）
    # 都会先调用它，保证后续建表、导入操作的已经是改名后的 vocab.db。
    if os.path.exists(OLD_DB_PATH):
        need_migrate = False
        if not os.path.exists(DB_PATH):
            need_migrate = True
        else:
            # 判断现有 vocab.db 是不是空壳：没有任何用户表的才算占位文件
            _probe = sqlite3.connect(DB_PATH)
            _n_tables = _probe.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            _probe.close()
            if _n_tables == 0:
                for suffix in ('', '-wal', '-shm'):
                    p = DB_PATH + suffix
                    if os.path.exists(p):
                        os.remove(p)
                need_migrate = True
        if need_migrate:
            for suffix in ('', '-wal', '-shm'):
                src = OLD_DB_PATH + suffix
                if os.path.exists(src):
                    os.rename(src, DB_PATH + suffix)
            print('已将旧数据库 cet6.db 平滑迁移为 vocab.db（文件改名，数据全部保留）')

    with open_db() as conn:
        # words 表结构：word 列不加 UNIQUE 约束，并带 book_id 字段。
        # 原因：多词书模式下，不同词书允许收录同一个单词（比如两本书都有 apple），
        # 全局唯一约束会阻止这种插入，所以建表时就直接去掉。
        # （老数据库的 UNIQUE 约束会在下面的"三步迁移"中平滑移除，见 upgrade 部分）
        conn.execute('''
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                phonetic TEXT DEFAULT '',
                meaning TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                last_review_time TEXT,
                next_review_time TEXT,
                review_stage INTEGER DEFAULT 0,
                book_id INTEGER DEFAULT 1
            )
        ''')
        conn.commit()

        # 学习日志表：记录用户每次点击"认识"/"不认识"的历史
        # 关键点：CREATE TABLE IF NOT EXISTS -> 兼容现有数据库，
        # 老的 cet6.db 没有这张表时启动会自动创建，旧数据完全不受影响
        # 字段说明：
        #   word_id    -> 关联 words 表的 id（单词可能以后被删，所以只做弱关联）
        #   word_text  -> 冗余保存单词拼写，保证单词被删后历史记录仍能正常显示
        #   study_date -> 学习日期，格式 YYYY-MM-DD，专门用于按天分组统计
        #   action     -> 操作类型：'认识' 或 '不认识'
        conn.execute('''
            CREATE TABLE IF NOT EXISTS study_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER,
                word_text TEXT NOT NULL,
                study_date TEXT NOT NULL,
                action TEXT NOT NULL,
                batch_num INTEGER DEFAULT 1
            )
        ''')

        # 【加练批次】平滑升级旧表：给 study_log 补 batch_num 列。
        # ALTER TABLE ADD COLUMN 只加列不动数据，旧记录自动填默认值 1（第1批），
        # 完全兼容旧数据，不需要清空任何东西。
        log_columns = [row[1] for row in conn.execute("PRAGMA table_info(study_log)")]
        if 'batch_num' not in log_columns:
            conn.execute("ALTER TABLE study_log ADD COLUMN batch_num INTEGER DEFAULT 1")

        # 【加练批次】历史记录去重迁移（不清空任何已有数据）：
        # 1) 清理重复行：同一个 (word_id, study_date, batch_num) 组合只保留 id 最大的那条
        #    （加了批次后，"同一天同单词"不再唯一，"同一天同单词同批次"才是唯一）
        # 2) 唯一索引换轴：从 (word_id, study_date) 换成 (word_id, study_date, batch_num)。
        #    旧索引必须删掉，否则加练时第2批写入会撞唯一约束报错！
        conn.execute('''
            DELETE FROM study_log
            WHERE id NOT IN (
                SELECT MAX(id) FROM study_log
                GROUP BY word_id, study_date, COALESCE(batch_num, 1)
            )
        ''')
        conn.execute('DROP INDEX IF EXISTS idx_study_log_word_date')
        conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_study_log_word_date_batch
            ON study_log(word_id, study_date, COALESCE(batch_num, 1))
        ''')
        conn.commit()

        # 自动升级旧表：检查并添加缺失字段（兼容老版本数据库，逐个补齐）
        columns = [row[1] for row in conn.execute("PRAGMA table_info(words)")]
        if 'next_review_time' not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN next_review_time TEXT")
            print('  已升级：添加 next_review_time 字段')
        if 'review_stage' not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN review_stage INTEGER DEFAULT 0")
            print('  已升级：添加 review_stage 字段')
        # 多词书支持：给旧表补 book_id 字段，默认 1（默认词书）
        # ALTER TABLE ADD COLUMN 带 DEFAULT 时，已有旧行会自动填上 1，
        # 所以原有的 3997 个单词全部自动归入默认词书，不需要清空数据库
        if 'book_id' not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN book_id INTEGER DEFAULT 1")
            print('  已升级：添加 book_id 字段（多词书支持）')
        # AI 评估相关两个字段，同样用 ALTER TABLE 平滑升级旧表：
        #   ai_score  -> AI 给单词打的重要性评分（1-5分），默认 0 表示"未经 AI 评估"
        #   ai_reason -> AI 的评估理由（30字以内），默认空字符串
        # ALTER TABLE ADD COLUMN 只加列、不动任何旧数据，老单词自动取默认值，
        # 所以无需清空 cet6.db，第二版的学习数据全部保留。
        if 'ai_score' not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN ai_score INTEGER DEFAULT 0")
            print('  已升级：添加 ai_score 字段（AI评分）')
        if 'ai_reason' not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN ai_reason TEXT DEFAULT ''")
            print('  已升级：添加 ai_reason 字段（AI评估理由）')
        # 多考试类型支持：
        # exam_type 标记这条评估针对哪种考试（六级/雅思/托福/考研）。
        # DEFAULT '六级' 让旧数据（包括之前已评估入库的词）自动归入"六级"，
        # 所以升级后老数据不会报错、也不会丢失"针对什么考试"的归属。
        if 'exam_type' not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN exam_type TEXT DEFAULT '六级'")
            print('  已升级：添加 exam_type 字段（多考试类型支持）')
        conn.commit()

        # 【关键升级】移除 word 列的 UNIQUE 约束：
        # 老表的 word 列是全局唯一的，不同词书无法收录同一个单词，必须去掉。
        # SQLite 不支持直接删除约束，标准做法是"三步迁移"（数据一条不丢）：
        #   1) 把旧表改名成 words_old
        #   2) 按新结构（无 UNIQUE、带 book_id）建一张新 words 表
        #   3) 把旧表数据原样复制进新表（book_id 为空的旧行用 COALESCE 补成 1）
        #   4) 删掉 words_old
        # 通过 PRAGMA index_list 检查 word 列是否带 UNIQUE 约束：
        #   row[2] 是 unique 标志(0/1)，row[3] 是来源（'u' 表示 UNIQUE 约束自动生成的索引）
        unique_word = any(
            row[2] == 1 and row[3] == 'u'
            for row in conn.execute("PRAGMA index_list(words)")
        )
        if unique_word:
            print('  检测到旧表 word 列带 UNIQUE 约束，正在升级为多词书结构...')
            conn.execute('ALTER TABLE words RENAME TO words_old')
            # 新表结构同步带上 ai_score / ai_reason / exam_type 列，
            # 否则从第一版带 UNIQUE 约束的老库迁移时，上面刚 ALTER 出来的
            # 新字段会在重建表过程中被丢掉
            conn.execute('''
                CREATE TABLE words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL,
                    phonetic TEXT DEFAULT '',
                    meaning TEXT DEFAULT '',
                    status TEXT DEFAULT 'new',
                    last_review_time TEXT,
                    next_review_time TEXT,
                    review_stage INTEGER DEFAULT 0,
                    book_id INTEGER DEFAULT 1,
                    ai_score INTEGER DEFAULT 0,
                    ai_reason TEXT DEFAULT '',
                    exam_type TEXT DEFAULT '六级'
                )
            ''')
            conn.execute('''
                INSERT INTO words (id, word, phonetic, meaning, status,
                                   last_review_time, next_review_time, review_stage, book_id,
                                   ai_score, ai_reason, exam_type)
                SELECT id, word, phonetic, meaning, status,
                       last_review_time, next_review_time, review_stage, COALESCE(book_id, 1),
                       COALESCE(ai_score, 0), COALESCE(ai_reason, ''),
                       COALESCE(exam_type, '六级')
                FROM words_old
            ''')
            conn.execute('DROP TABLE words_old')
            conn.commit()
            print('  words 表升级完成，原有数据全部保留')

        # 词书表：记录所有的词书（默认词书 + 上传的词书）
        conn.execute('''
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,        -- 词书名，如 "默认六级词库"、"上传词书_20261006"
                created_at TEXT NOT NULL   -- 创建时间
            )
        ''')
        conn.commit()

        # 【兼容旧数据】books 表为空 -> 自动补一条默认词书（固定 id=1）
        # 这样老的 cet6.db 升级后，已有单词（book_id 默认 1）都有归属，平滑过渡
        if conn.execute('SELECT COUNT(*) FROM books').fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO books (id, name, created_at) VALUES (1, '默认六级词库', ?)",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
            )
            conn.commit()
            print('  已创建默认词书：默认六级词库（id=1）')

        # 【改造】确保 4 本 AI 分类词书（六级/雅思/托福/考研）都存在：
        # 逐本按名字查 books 表 -> 查不到就插入，已存在则跳过（幂等，重复启动不会重复建书）。
        # 这样无论第几版数据库，启动后每个考试类别都有独立的存词位置。
        for exam_type, ai_book_name in AI_BOOK_NAMES.items():
            row = conn.execute(
                'SELECT id FROM books WHERE name = ?', (ai_book_name,)
            ).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO books (name, created_at) VALUES (?, ?)',
                    (ai_book_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                )
                print(f'  已创建AI评估词书：{ai_book_name}')
        conn.commit()

        # 【数据兼容】旧版把所有 AI 评估词混存在单一的"AI评估词库"里。
        # 升级后把这本旧书里的单词一次性迁移到"AI评估词库-六级"（旧版遗留统一归入六级），
        # 迁移完删除空壳旧书，避免词书管理页出现一本永远用不到的书。
        # 幂等性：旧书不存在（首次启动已完成迁移）时整段直接跳过，零副作用。
        legacy_row = conn.execute(
            "SELECT id FROM books WHERE name = 'AI评估词库'"
        ).fetchone()
        if legacy_row is not None:
            six_row = conn.execute(
                'SELECT id FROM books WHERE name = ?', (AI_BOOK_NAMES['六级'],)
            ).fetchone()
            if six_row is not None:
                # 只搬运单词（words.book_id 改指向六级分类词书），study_log 完全不碰
                conn.execute(
                    'UPDATE words SET book_id = ? WHERE book_id = ?',
                    (six_row['id'], legacy_row['id'])
                )
                conn.execute('DELETE FROM books WHERE id = ?', (legacy_row['id'],))
                conn.commit()
                print('  数据兼容：旧"AI评估词库"的单词已迁移到 AI评估词库-六级，旧书已移除')

        # 保险：万一有单词的 book_id 是空的（比如手工操作过数据库），统一归入默认词书
        conn.execute('UPDATE words SET book_id = 1 WHERE book_id IS NULL')
        conn.commit()

        # 迁移旧状态值 unknown -> learning
        conn.execute("UPDATE words SET status = 'learning' WHERE status = 'unknown'")
        conn.commit()

        # 检查是否需要导入数据
        count = conn.execute('SELECT COUNT(*) FROM words').fetchone()[0]
        if count == 0:
            print('数据库为空，开始从文件导入单词...')
            load_words_from_file(conn)
        else:
            print(f'数据库已有 {count} 个单词')
    # 【重构说明】原来的 conn.close() 已删除：离开 with 块时 open_db 自动关闭


def has_chinese(text):
    """
    判断字符串是否包含中文字符。
    原理：逐个字符检查 Unicode 码位是否落在 CJK 统一汉字区（\\u4e00 ~ \\u9fff），
    这个区间覆盖所有常用中文汉字；英文单词、数字、连字符（-）、撇号（'）
    都不在这个区间里，所以"纯英文的单词列"不会误判。
    """
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


def parse_word_line(line):
    """
    公共函数：解析一行文本，提取出（单词, 释义）。
    供"初始导入"和"上传词书"两处共用，保证解析规则完全一致。

    兼容两种分隔符：Tab 或 逗号
    兼容两种格式：  [单词, 释义] 或 [序号, 单词, 释义]
    解析失败返回 None（空行、只有单词没有释义的行都算无效）
    """
    line = line.strip()
    if not line:
        return None

    # 优先按 Tab 分隔（六级词书是 Tab 分隔），一行里没有 Tab 再按逗号分隔
    if '\t' in line:
        sep = '\t'
    else:
        sep = ','
    parts = [p.strip() for p in line.split(sep)]
    # 去掉空段（例如行尾多打了一个逗号、序号列是空的情况）
    parts = [p for p in parts if p]

    if len(parts) >= 2 and parts[0].isdigit():
        # 格式：[序号, 单词, 释义...] -> 第1列是纯数字序号，跳过它，取第2列做单词
        # 释义把第3列及之后的内容重新拼接回去（防止释义本身含逗号被切碎）
        # 注意：只有序号列是"纯数字"才跳过；释义里带逗号的行（如 combination,n.结合,组合）
        # 第一列是英文单词，绝不能当成序号丢掉 —— 这正是 Bug2 错位的来源之一
        if len(parts) == 2:
            # [序号, 单词] 少了释义列（如 "3,incomplete"），当作无效行跳过
            return None
        word = parts[1]
        meaning = sep.join(parts[2:])
    elif len(parts) == 1:
        # 只有1列（光有单词没有释义）等无效行，直接跳过
        return None
    else:
        # 常规格式：第1列就是单词，后面全部拼回释义
        word = parts[0]
        meaning = sep.join(parts[1:])

        # ---- 列错位保护：'单词'列里绝对不能出现中文 ----
        # 正常情况：parts[0] 是英文单词，parts[1] 起是中文释义
        # 异常情况：整行写反了，如 "组合,combination"（释义跑到了单词列）
        if has_chinese(word):
            if len(parts) >= 2 and not has_chinese(parts[1]):
                # parts[0] 是中文、parts[1] 是英文 -> 说明两列写反了：
                # 把 parts[1] 当作单词，parts[0] 和 parts[2] 及之后的内容合并回释义
                word = parts[1]
                meaning = sep.join([parts[0]] + parts[2:])
            else:
                # 两列都是中文（如 "你好,世界"）：这行不可能是合法词条，跳过
                return None

    if not word or not meaning:
        return None
    return word, meaning


def merge_or_insert_word(conn, word, meaning, book_id=1):
    """
    把一个单词写入数据库（初始导入和上传词书共用的去重逻辑）。
    book_id 参数：去重只在"同一本词书内"进行，
    不同词书之间允许存在同一个单词（互不影响、释义不混在一起）。
    规则：
    - 本词书中已存在该单词 -> 不重复插入，把新释义追加到旧释义后（用 " / " 分隔）
    - 本词书中不存在 -> 插入新单词（status 默认 'new'，走正常学习流程）
    返回 'inserted'（新增）或 'merged'（合并），方便调用方统计数量。
    """
    # 先查这本词书里这个单词是否已存在（上传的文件里可能有重复词）
    existing = conn.execute(
        'SELECT meaning FROM words WHERE word = ? AND book_id = ?',
        (word, book_id)
    ).fetchone()
    if existing:
        # 已存在：把新释义追加到旧释义后面，用 " / " 分隔
        merged_meaning = existing['meaning'] + ' / ' + meaning
        conn.execute(
            'UPDATE words SET meaning = ? WHERE word = ? AND book_id = ?',
            (merged_meaning, word, book_id)
        )
        return 'merged'
    else:
        # 新单词：正常插入，带上所属词书 ID
        conn.execute(
            'INSERT INTO words (word, phonetic, meaning, book_id) VALUES (?, ?, ?, ?)',
            (word, '', meaning, book_id)
        )
        return 'inserted'


def load_words_from_file(conn):
    """从数据文件读取单词并导入数据库"""
    words_inserted = 0
    file_handle = None
    for encoding in ['utf-8', 'gbk', 'utf-8-sig']:
        try:
            file_handle = open(DATA_FILE, 'r', encoding=encoding)
            file_handle.readline()
            file_handle.seek(0)
            print(f'  成功用 {encoding} 编码打开文件')
            break
        except UnicodeDecodeError:
            if file_handle:
                file_handle.close()
            file_handle = None
            continue

    if file_handle is None:
        print('错误：无法读取数据文件，请检查文件编码')
        return

    for line in file_handle:
        # 解析行 -> 去重写入这两步已抽成公共函数，
        # 和 /upload 上传词书共用同一套逻辑
        result = parse_word_line(line)
        if result is None:
            continue
        word, meaning = result
        if merge_or_insert_word(conn, word, meaning, book_id=1) == 'inserted':
            words_inserted += 1

    file_handle.close()
    conn.commit()
    print(f'  成功导入 {words_inserted} 个单词到数据库（重复词已合并释义）')


# ====================================================================
# 设置和进度文件读写
# ====================================================================

def load_daily_goal():
    """读取每日新词目标，默认 DEFAULT_DAILY_GOAL（配置区可改）"""
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            goal = int(f.read().strip())
            return goal if goal >= 1 else DEFAULT_DAILY_GOAL
    except (FileNotFoundError, ValueError):
        return DEFAULT_DAILY_GOAL


def save_daily_goal(goal):
    """保存每日新词目标"""
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        f.write(str(goal))


def load_current_book_id():
    """
    读取当前选中的词书 ID（current_book.txt 里存的就是一个数字，如 "1"）。
    两层兜底，保证任何情况下都能返回一个可用的词书 ID：
    1. 文件不存在 / 内容不是数字 -> 返回 1（默认词书）
    2. 文件里的 ID 在 books 表里不存在（比如库被重建过）-> 也返回 1
    """
    try:
        with open(CURRENT_BOOK_FILE, 'r', encoding='utf-8') as f:
            book_id = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 1
    # 再到数据库里确认这本词书真的存在
    with open_db() as conn:
        exists = conn.execute('SELECT id FROM books WHERE id = ?', (book_id,)).fetchone()
    if exists:
        return book_id
    return 1


def save_current_book_id(book_id):
    """把当前选中的词书 ID 写入 current_book.txt"""
    with open(CURRENT_BOOK_FILE, 'w', encoding='utf-8') as f:
        f.write(str(book_id))


def get_current_book(conn):
    """
    一次性拿到"当前词书"的信息，返回 (book_id, book_name) 元组。
    首页/复习页/词书管理页都要用到，统一在这里取，避免重复代码。
    """
    book_id = load_current_book_id()
    row = conn.execute('SELECT name FROM books WHERE id = ?', (book_id,)).fetchone()
    if row:
        return book_id, row['name']
    # 理论上到不了这里（load 里已兜底），防御性写法
    return book_id, '未知词书'


def load_progress_entry(book_id):
    """
    【加练批次】读取指定词书"今天"的进度条目，返回 (批次, 当前批次已学数量)。

    进度文件每行一条记录，新格式（4段）：
        日期|词书ID|批次|数量    例如 2026-10-06|1|2|15
        表示：10月6日、词书1、第2次学习（加练的第1轮）、当前批次已学15个

    兼容性：
    - 旧格式（3段）日期|词书ID|数量 -> 批次按 1 处理（旧记录都是第1批）
    - 找不到今天的记录（跨天/切书/第一次学习）-> 返回 (1, 0)，相当于从第1批重新开始
    """
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        with open(DAILY_PROGRESS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 3 and parts[0] == today and parts[1] == str(book_id):
                    try:
                        count = int(parts[-1])                            # 数量固定取最后一段
                        batch = int(parts[2]) if len(parts) == 4 else 1   # 3段旧格式批次=1
                        return batch, count
                    except ValueError:
                        # 某一段被手动改坏（非数字）：按全新进度处理，不让程序崩溃
                        return 1, 0
    except (FileNotFoundError, OSError):
        pass  # 文件还不存在（第一次运行）
    return 1, 0


def save_progress_entry(book_id, batch, count):
    """
    【加练批次】写入指定词书"今天"的进度（batch, count）。
    写入策略：保留"今天 + 其他词书"的行（多词书进度并存，互不覆盖），
    丢弃过期日期的行，最后把当前词书的新记录（4段格式）追加进去。
    """
    today = datetime.now().strftime('%Y-%m-%d')
    kept_lines = []
    try:
        with open(DAILY_PROGRESS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                # 只保留"今天 + 其他词书"的行：过期日期行顺手清理，当前词书的旧行丢弃（马上重写）
                if len(parts) >= 3 and parts[0] == today and parts[1] != str(book_id):
                    kept_lines.append(line.strip())
    except (FileNotFoundError, OSError):
        pass  # 文件还不存在，kept_lines 保持为空即可

    kept_lines.append(f'{today}|{book_id}|{batch}|{count}')
    with open(DAILY_PROGRESS_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(kept_lines) + '\n')


def load_new_words_today(book_id):
    """
    【加练批次】返回"当前批次已学的新词数量"。
    批次信息由 load_progress_entry 一并提供，这里只需要数量。
    新的一天/切换词书/第一次学习 -> 自然返回 0，第1批从头计数。
    """
    batch, count = load_progress_entry(book_id)
    return count


def increment_new_words_today(book_id):
    """
    【加练批次】"当前批次已学新词数量" +1（批次号保持不变）。
    注意：批次只在 /continue_learning 路由里 +1，普通学习不碰批次。
    """
    batch, count = load_progress_entry(book_id)
    save_progress_entry(book_id, batch, count + 1)


# ====================================================================
# 公共统计函数（新词模式和复习模式共用）
# ====================================================================

def count_review_pending(conn, now_str, book_id):
    """
    【核心重构③】统计指定词书中"到期待复习"的单词数量。
    判定口径：next_review_time 已到期（或为空，兼容旧版迁移数据）
    且 status = 'learning'，且属于指定词书。
    原来这段 SQL 在 get_common_stats / books / history / ai_book 四处逐字重复，
    现在统一抽成这一个函数——以后想调整"什么算待复习"，只需要改这里。
    """
    return conn.execute(
        """SELECT COUNT(*) FROM words
           WHERE (next_review_time <= ? OR next_review_time IS NULL)
           AND status = 'learning'
           AND book_id = ?""",
        (now_str, book_id)
    ).fetchone()[0]


def get_common_stats(conn, now_str, book_id=1):
    """
    获取两个模式共用的统计数据。
    book_id 参数：所有统计都只统计"当前词书"的单词，
    切换词书后，各页面的总进度/学习中/待复习数字都会跟着变。
    """
    known_count = conn.execute(
        "SELECT COUNT(*) FROM words WHERE status = 'known' AND book_id = ?",
        (book_id,)
    ).fetchone()[0]
    learning_count = conn.execute(
        "SELECT COUNT(*) FROM words WHERE status = 'learning' AND book_id = ?",
        (book_id,)
    ).fetchone()[0]
    new_count = conn.execute(
        "SELECT COUNT(*) FROM words WHERE status = 'new' AND book_id = ?",
        (book_id,)
    ).fetchone()[0]
    total_count = conn.execute(
        "SELECT COUNT(*) FROM words WHERE book_id = ?",
        (book_id,)
    ).fetchone()[0]

    # 待复习数量：【重构】复用上面的公共统计函数（SQL 与原来逐字相同）
    review_pending = count_review_pending(conn, now_str, book_id)

    return {
        'known_count': known_count,
        'learning_count': learning_count,
        'new_count': new_count,
        'total_count': total_count,
        'review_pending': review_pending,
    }


def get_page_context(conn):
    """
    【核心重构③】首页(/)和复习页(/review)共用的页面数据。
    原来这两个路由各自重复读取：当前词书 / 每日目标 / 今日进度 / 公共统计 /
    进度百分比，现在统一在这里取一次，返回的字典在 render_template 里 ** 展开。
    多出来的 book_id 键只是方便路由内部继续使用（模板不认识它也不会报错）。
    """
    book_id, book_name = get_current_book(conn)
    daily_goal = load_daily_goal()
    new_words_today = load_new_words_today(book_id)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    stats = get_common_stats(conn, now_str, book_id)
    # 预计算进度百分比，传给模板用（避免在HTML的CSS里写Jinja2表达式）
    progress_percent = (new_words_today * 100 // daily_goal) if daily_goal > 0 else 0

    return {
        'book_id': book_id,
        'current_book_name': book_name,
        'daily_goal': daily_goal,
        'new_words_today': new_words_today,
        'progress_percent': progress_percent,
        **stats,
    }


# ====================================================================
# AI 单词评估相关函数
# ====================================================================

def get_ai_book_id(conn, exam_type=DEFAULT_EXAM_TYPE):
    """
    【改造】按考试类型返回对应 AI 词书的 book_id（每个考试类别一本，互不混放）。

    参数：
      exam_type：考试类型（六级/雅思/托福/考研）。
                 调用方（/ai_book、/ai_evaluate）已用 EXAM_TYPES 白名单校验过；
                 这里再用 AI_BOOK_NAMES.get 兜底，非法值自动回退"六级"，
                 保证永远能拿到一个合法的词书名，绝不返回 None。

    init_database() 启动时已保证 4 本书都存在，这里正常都能查到；
    万一某本被手动删了，就当场补建同名书，保证后续逻辑永远拿得到 ID。
    """
    book_name = AI_BOOK_NAMES.get(exam_type, AI_BOOK_NAMES[DEFAULT_EXAM_TYPE])
    row = conn.execute(
        'SELECT id FROM books WHERE name = ?', (book_name,)
    ).fetchone()
    if row is not None:
        return row['id']
    # 兜底：补建这本词书并拿到新 id（lastrowid 是刚插入行的自增主键）
    cursor = conn.execute(
        'INSERT INTO books (name, created_at) VALUES (?, ?)',
        (book_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    return cursor.lastrowid


def get_ai_evaluation(word, exam_type=DEFAULT_EXAM_TYPE):
    """
    调用大模型评估一个英文单词在【指定考试】中的重要性。

    参数：
      word      ：要评估的英文单词
      exam_type ：考试类型（六级/雅思/托福/考研），默认"六级"；
                  不在白名单 EXAM_TYPES 里时一律按默认类型处理，
                  保证 Prompt 里永远不会拼进奇怪的内容

    流程：
      1. 组装请求：动态 System Prompt（[考试]专家人设 + 只输出JSON）+ 用户消息（单词）
      2. requests.post 发请求，必须带 verify=False（防杀毒软件拦截）+ timeout
      3. 从 choices[0].message.content 取出 AI 回复文本
      4. 容错解析 JSON：AI 有时会用 ```json ... ``` 代码块包裹，或在 JSON 前后
         多说一两句解释，所以先去掉代码块标记，再截取第一个 '{' 到最后一个 '}'
      5. 校验三个字段 meaning / score / reason，score 强制夹在 1~5 之间
    返回：{'meaning': 中文释义, 'score': 1~5整数, 'reason': 评估理由}
    异常：网络错误、HTTP 错误、JSON 解析失败、字段缺失等情况全部抛出异常，
          由调用方（/ai_evaluate 路由）统一捕获并 flash 给用户。
    """
    # 白名单兜底：只允许预设的考试类型，非法值回落到默认"六级"
    if exam_type not in EXAM_TYPES:
        exam_type = DEFAULT_EXAM_TYPE

    # 关键：System Prompt 按本次考试类型动态生成（同一个词、不同考试、不同人设）
    # 【重构】Prompt 模板已移到顶部配置区 AI_SYSTEM_PROMPT_TEMPLATE
    system_prompt = AI_SYSTEM_PROMPT_TEMPLATE.format(exam_type=exam_type)

    # ---- 第1步：组装请求头和请求体（OpenAI 兼容的 chat/completions 格式）----
    headers = {
        'Authorization': f'Bearer {API_KEY}',   # 固定写法：Bearer + 空格 + 密钥
        'Content-Type': 'application/json',
    }
    payload = {
        'model': MODEL_NAME,
        'messages': [
            {'role': 'system', 'content': system_prompt},  # 动态人设与输出格式要求
            {'role': 'user', 'content': word},             # 本轮只问一个单词
        ],
        # 温度取配置区的 AI_TEMPERATURE（0.3，评分任务求稳）
        'temperature': AI_TEMPERATURE,
    }

    # ---- 第2步：发请求 ----
    # verify=False：不校验 SSL 证书（关键，否则本机杀毒软件代理会导致报错）
    # timeout 取配置区的 AI_TIMEOUT：超时抛异常，避免页面一直转圈卡死
    response = requests.post(
        API_URL, headers=headers, json=payload,
        timeout=AI_TIMEOUT, verify=False
    )
    response.raise_for_status()   # 密钥错误/接口异常（状态码非200）时在这里抛异常

    # ---- 第3步：取出 AI 回复的文本内容 ----
    content = response.json()['choices'][0]['message']['content'].strip()

    # ---- 第4步：容错提取 JSON 字符串 ----
    # 4.1 去掉可能存在的 ```json ... ``` 代码块围栏（大小写都兼容）
    content = re.sub(r'^```(?:json)?|```$', '', content, flags=re.IGNORECASE).strip()
    # 4.2 截取第一个 '{' 到最后一个 '}' 之间的内容，
    #     防住 AI 在 JSON 前后夹带"好的，评估结果如下："这类废话
    start = content.find('{')
    end = content.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f'AI 返回内容中找不到 JSON：{content[:100]}')
    data = json.loads(content[start:end + 1])   # 解析失败会抛 json.JSONDecodeError

    # ---- 第5步：字段校验与规范化 ----
    meaning = str(data.get('meaning', '')).strip()
    reason = str(data.get('reason', '')).strip()
    if not meaning or not reason:
        # 释义或理由为空，说明 AI 没按要求输出，视为失败
        raise ValueError(f'AI 返回的 JSON 缺少必要字段：{data}')
    try:
        score = int(data.get('score', 0))
    except (TypeError, ValueError):
        # score 不是数字（比如返回了 "五星"）
        raise ValueError(f'AI 返回的评分不是数字：{data.get("score")}')
    # 强制夹在 1~5：AI 偶尔会给 0 或 6，统一纠正到合法区间
    score = max(1, min(5, score))

    return {'meaning': meaning, 'score': score, 'reason': reason}


# ====================================================================
# 路由
# ====================================================================

@app.route('/')
def index():
    """
    首页 = 新词学习模式（GET请求）
    逻辑：
    1. 读取每日新词目标和今天已学新词数
    2. 如果今天还没学够目标数，且有新词可学 -> 抽一个新词
    3. 如果新词目标已达成或没有新词了 -> 不抽词，显示完成提示
    """
    with open_db() as conn:
        # 【重构】当前词书/每日目标/今日进度/统计数字等公共数据，
        # 统一由 get_page_context 一次取齐（原来首页和复习页各写一遍）
        ctx = get_page_context(conn)
        book_id = ctx['book_id']

        # ---- 判断是否还能学新词 ----
        # 条件1：今天在这个词书里学的新词还没到目标
        # 条件2：当前词书里还有新词（status='new'）
        can_learn_new = (ctx['new_words_today'] < ctx['daily_goal']) and (ctx['new_count'] > 0)

        word = None
        new_task_done = False  # 新词任务是否完成

        if can_learn_new:
            # ---- 【Bug1修复】优先使用 session 里记住的"当前新词" ----
            # 之前每次进首页都 ORDER BY RANDOM() 重抽，切到别的页面再回来
            # 正在看的单词就变了。现在改为：
            # 1. session 里有 current_word_id -> 先按 id 查库，查到就直接用它
            # 2. 查询带上三重校验：单词必须存在 + 状态还是 'new' + 属于当前词书，
            #    任何一个条件不满足（学掉了/删了/切了词书）都会查不到 -> 走重抽
            # 3. session 里没有（刚打开/刚提交完/刚切词书）-> 才随机抽一个新词，
            #    并把它的 id 记进 session，之后切页面回来它就不会变了
            saved_id = session.get('current_word_id')
            if saved_id:
                word = conn.execute(
                    """SELECT * FROM words
                       WHERE id = ? AND status = 'new' AND book_id = ?""",
                    (saved_id, book_id)
                ).fetchone()

            if word is None:
                # 没有记住的词（或记住的词已失效）：随机抽一个新词（只从当前词书里抽）
                word = conn.execute(
                    "SELECT * FROM words WHERE status = 'new' AND book_id = ? "
                    "ORDER BY RANDOM() LIMIT 1",
                    (book_id,)
                ).fetchone()
                if word is not None:
                    # 记住这个新抽的词的 id，Flask 的 session 存在加密 Cookie 里，
                    # 无需装任何包（secret_key 已在配置区设置）
                    session['current_word_id'] = word['id']
        else:
            new_task_done = True

    # 判断"当前词书是否已学完"：这本书里一个未学新词都不剩了。
    # 注意和 new_task_done（今日目标达成）是两回事：
    #   new_task_done  -> 今天学够了目标数，明天再来学
    #   book_finished  -> 这本书整个学完了，该去换新书了
    # book_finished 优先级更高，模板里优先显示"切换词书"的引导
    book_finished = (ctx['new_count'] == 0)

    # 【加练】当前批次号："继续学习"按钮文案用 ——
    # 第1轮显示"今日第1次加练"，加练后批次变2，显示"今日第2次加练"，以此类推
    current_batch, _ = load_progress_entry(book_id)

    return render_template(
        'index.html',
        word=word,
        mode='new',               # 告诉模板：当前是新词模式
        new_task_done=new_task_done,
        current_batch=current_batch,          # 【加练】当前批次号
        book_finished=book_finished,          # 当前词书是否已学完
        **ctx                     # 展开：词书名/目标/进度/统计等全部模板变量
    )


@app.route('/review')
def review():
    """
    复习模式（GET请求）
    逻辑：
    1. 查询所有 next_review_time <= 现在 的单词（到该复习了）
    2. 如果有，随机抽一个显示
    3. 如果没有，显示"今日复习完毕"
    """
    with open_db() as conn:
        # 【重构】公共数据统一来自 get_page_context
        ctx = get_page_context(conn)

        word = None
        review_done = False  # 复习是否完成

        if ctx['review_pending'] > 0:
            # 抽一个到期需要复习的词（只从当前词书里抽）
            word = conn.execute(
                """SELECT * FROM words
                   WHERE (next_review_time <= ? OR next_review_time IS NULL)
                   AND status = 'learning'
                   AND book_id = ?
                   ORDER BY RANDOM() LIMIT 1""",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ctx['book_id'])
            ).fetchone()
            # 如果查不到（理论上不该发生，但保险起见）
            if word is None:
                review_done = True
        else:
            review_done = True

    return render_template(
        'index.html',
        word=word,
        mode='review',            # 告诉模板：当前是复习模式
        review_done=review_done,
        **ctx
    )


@app.route('/submit', methods=['POST'])
def submit():
    """
    提交路由（POST请求）
    处理用户点击"认识"或"不认识"
    - 如果这个词之前是 'new' 状态（第一次学），今天新词计数+1
    - 应用艾宾浩斯算法更新复习阶段和下次复习时间
    - 同时向 study_log 表写入/更新一条学习日志，供 /history 页面按天查看
    - 根据来源模式重定向：新词模式回 /，复习模式回 /review
    """
    word_id = request.form.get('word_id')
    action = request.form.get('action')
    mode = request.form.get('mode', 'new')   # 隐藏字段，记录来自哪个模式

    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    today_str = now.strftime('%Y-%m-%d')     # 日志只精确到"天"，用于按天分组

    with open_db() as conn:
        word = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()

        if word:
            # ---- 关键：如果是新词第一次学，今日新词计数+1 ----
            # 传入该单词所属的词书ID（用 word['book_id'] 而不是当前词书ID，
            # 保证计数永远记在单词真正所在的那本书头上）
            if word['status'] == 'new':
                increment_new_words_today(word['book_id'])

            current_stage = word['review_stage'] if word['review_stage'] is not None else 0

            # ---- 【核心重构③】合并重复 UPDATE ----
            # 原来"认识"/"不认识"两个分支里各写了一段一模一样的 UPDATE 语句，
            # 现在改为：分支里只计算参数，最后统一执行一次 UPDATE。
            # 每个分支算出的参数值和原来逐字一致，业务行为不变。
            if action == '认识':
                # 认识：进入下一复习阶段
                new_stage = current_stage + 1
                interval_days = REVIEW_INTERVALS.get(new_stage, 30)
                next_review = (now + timedelta(days=interval_days)).strftime('%Y-%m-%d %H:%M:%S')
                # 到达最大阶段 -> 标记为已掌握，否则继续"学习中"
                new_status = 'known' if new_stage >= MAX_STAGE else 'learning'
            elif action == '不认识':
                # 不认识：重置阶段，10分钟后再复习
                new_stage = 0
                next_review = (now + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
                new_status = 'learning'

            # 写入学习日志 + 执行单词状态更新（只在 action 合法时做，
            # 与原来的 if/elif + if action in (...) 双重守卫完全等价）
            if action in ('认识', '不认识'):
                # 单词状态更新（原来两段相同的 UPDATE 合并成这一段）
                conn.execute(
                    """UPDATE words
                       SET status = ?, review_stage = ?, last_review_time = ?, next_review_time = ?
                       WHERE id = ?""",
                    (new_status, new_stage, now_str, next_review, word_id)
                )

                # 学习日志去重原则：同一天 + 同一个单词，只保留一条记录，
                # 以最后一次点击的状态为准（配合 init_database 里的唯一索引双层保证）
                # 批次号取自"单词所属词书"的进度文件（按所属词书取最严谨）
                word_batch, _ = load_progress_entry(word['book_id'])
                # 去重维度：同一天 + 同一单词 + 同一批次 才算同一条记录。
                # 加练后（批次2）重新学同一个单词，会新增一条记录，不会覆盖第1批的记录
                existing_log = conn.execute(
                    'SELECT id FROM study_log WHERE word_id = ? AND study_date = ? AND batch_num = ?',
                    (word_id, today_str, word_batch)
                ).fetchone()
                if existing_log:
                    # 同一批次里已学过：只更新那条记录的动作，不新增行
                    conn.execute(
                        'UPDATE study_log SET action = ? WHERE id = ?',
                        (action, existing_log['id'])
                    )
                else:
                    # 这个批次里第一次学：插入新记录，带上批次号
                    conn.execute(
                        '''INSERT INTO study_log (word_id, word_text, study_date, action, batch_num)
                           VALUES (?, ?, ?, ?, ?)''',
                        (word_id, word['word'], today_str, action, word_batch)
                    )

        conn.commit()

    # 【Bug1修复】提交完"认识/不认识"后，清掉 session 里记住的当前新词。
    # 不清的话，回到首页会一直显示刚才那个词（它的状态已不是 new，
    # 虽然校验会兜底重抽，但显式清空语义更清晰）。
    # （与原顺序一致：先关闭数据库连接——离开 with 块——再操作 session）
    session.pop('current_word_id', None)

    # 根据来源模式决定重定向到哪
    if mode == 'review':
        return redirect(url_for('review'))
    else:
        return redirect(url_for('index'))


@app.route('/settings', methods=['POST'])
def settings():
    """保存每日新词目标"""
    goal = request.form.get('daily_goal', DEFAULT_DAILY_GOAL)
    try:
        goal = int(goal)
        if goal < 1:
            goal = 1
    except (ValueError, TypeError):
        goal = DEFAULT_DAILY_GOAL
    save_daily_goal(goal)
    return redirect(url_for('index'))


@app.route('/upload', methods=['POST'])
def upload():
    """
    上传自定义词书（POST请求）—— 多词书版
    完整流程：
    1. 校验文件：有没有选择文件、后缀是否为 .txt / .csv
    2. 读取文件字节流，依次尝试 utf-8-sig / utf-8 / gbk 三种编码解码
    3. 按行解析（兼容 Tab 或 逗号 分隔），得到（单词, 释义）列表
    4. 先在 books 表创建一本新词书，拿到新的 book_id，
       然后把解析出的单词全部插入 words 表并带上这个 book_id，
       单词去重（合并释义）只在新词书内部进行
    5. 上传成功后自动把"当前词书"切换到这本新词书
    6. 任何一步出错都 flash 错误提示并重定向回首页；成功则 flash 成功提示
    """
    # ---- 第1步：取出上传的文件对象 ----
    # request.files 是 Flask 自带的文件上传处理，不需要额外安装任何包
    file = request.files.get('wordbook')

    # 校验：用户没选文件就点了"上传词书"按钮
    if file is None or file.filename == '':
        flash('上传失败：请先选择一个 .txt 或 .csv 文件', 'error')
        return redirect(url_for('index'))

    # ---- 第2步：只允许 .txt / .csv 两种扩展名 ----
    filename = file.filename
    if not filename.lower().endswith(('.txt', '.csv')):
        flash('上传失败：只支持 .txt 或 .csv 文件', 'error')
        return redirect(url_for('index'))

    # ---- 第3步：读取文件内容，尝试多种编码解码 ----
    # 上传过来的文件是二进制流，必须先 decode 成字符串才能按行解析。
    # utf-8-sig 会自动去掉 Windows 记事本保存 UTF-8 时加的 BOM 头；
    # gbk 兜底处理老的中文编码文件。
    raw_bytes = file.read()
    text = None
    for encoding in ['utf-8-sig', 'utf-8', 'gbk']:
        try:
            text = raw_bytes.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        flash('上传失败：无法识别文件编码，请用记事本另存为 UTF-8 后重试', 'error')
        return redirect(url_for('index'))

    # ---- 第4步：按行解析出（单词, 释义）列表 ----
    parsed_words = []
    for line in text.splitlines():
        result = parse_word_line(line)   # 复用公共解析函数（Tab/逗号兼容）
        if result is not None:
            parsed_words.append(result)

    # 校验：一行有效单词都没解析到（空文件或格式全不对）
    if not parsed_words:
        flash('上传失败：文件中没有解析到有效单词。'
              '每行格式应为"单词,释义"或"序号,单词,释义"', 'error')
        return redirect(url_for('index'))

    # ---- 第5步：创建新词书，把单词全部插进这本书 ----
    # 优先使用用户在上传表单里填的自定义名称（book_name 字段，可选）：
    # request.form 用来取普通文本字段（区别于 request.files 的文件字段），
    # strip() 去掉首尾空格；填了就用（最长50字符防止刷屏），没填就沿用
    # 默认命名规则"上传词书_日期"（如 上传词书_20261006）
    custom_name = request.form.get('book_name', '').strip()
    if custom_name:
        book_name = custom_name[:50]
    else:
        book_name = '上传词书_' + datetime.now().strftime('%Y%m%d')
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted_count = 0   # 新增的单词数
    merged_count = 0     # 词书内部重复单词的合并数

    # 【重构说明】原来这里要手写 try/finally conn.close()；
    # 现在 with 块保证：无论成功、报错还是中途 return，连接都会自动关闭
    with open_db() as conn:
        try:
            # 5.1 先往 books 表插入新词书记录
            # cursor.lastrowid 可以拿到刚 INSERT 这一行自动生成的 id，
            # 后面插入的每个单词都要带上它作为 book_id
            cursor = conn.execute(
                'INSERT INTO books (name, created_at) VALUES (?, ?)',
                (book_name, created_at)
            )
            new_book_id = cursor.lastrowid

            # 5.2 把解析出的单词全部写入 words 表，带上新词书的 book_id
            # 注意：merge_or_insert_word 传入了 new_book_id，
            # 所以"已存在就合并释义"的判断只发生在新词书内部，
            # 不会和默认词库或其他词书里的同名单词互相干扰
            for word, meaning in parsed_words:
                if merge_or_insert_word(conn, word, meaning, book_id=new_book_id) == 'inserted':
                    inserted_count += 1
                else:
                    merged_count += 1
            conn.commit()   # 建书+插词全部写完再统一提交，保证要么全成功要么全不写
        except sqlite3.Error:
            # 数据库写入出错：回滚本次全部写入（新建的词书记录也会一起回滚，
            # 不会留下一本空词书），避免只写一半留下脏数据
            conn.rollback()
            flash('上传失败：数据库写入出错，请重试', 'error')
            return redirect(url_for('index'))

    # ---- 第6步：自动切换当前词书到新词书 ----
    # 用户上传词书的目的通常就是想学这本，所以直接帮他切换好
    save_current_book_id(new_book_id)

    # ---- 第7步：成功提示（flash 消息会显示在首页顶部，只显示一次）----
    flash(f'上传成功：新词书「{book_name}」已创建并自动切换，'
          f'共 {inserted_count + merged_count} 个单词'
          f'（新增 {inserted_count}，合并 {merged_count} 条重复释义）',
          'success')
    return redirect(url_for('index'))


# ====================================================================
# 多词书管理路由
# ====================================================================

@app.route('/books')
def books():
    """
    词书管理页（GET请求）
    查询 books 表展示所有词书，并统计每本词书的单词总数。
    LEFT JOIN 保证"一本空词书（还没有单词）"也能出现在列表里（词数为 0）。
    GROUP BY b.id 把每本词书的多行 words 统计成一个 COUNT。
    """
    current_book_id = load_current_book_id()
    with open_db() as conn:
        book_list = conn.execute('''
            SELECT b.id,
                   b.name,
                   b.created_at,
                   COUNT(w.id) AS word_count
            FROM books b
            LEFT JOIN words w ON w.book_id = b.id
            GROUP BY b.id, b.name, b.created_at
            ORDER BY b.id ASC
        ''').fetchall()

        # 给统一导航栏的"复习（X）"标签提供待复习数量（只算当前词书）
        # 【重构】SQL 已抽到公共函数 count_review_pending
        book_id, _ = get_current_book(conn)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        review_pending = count_review_pending(conn, now_str, book_id)

    # current_book_id 传给模板，用来高亮"当前学习中"的那本
    # mode='books' 告诉共享导航栏：当前页是词书管理
    return render_template('books.html',
                           book_list=book_list,
                           current_book_id=current_book_id,
                           mode='books',
                           review_pending=review_pending)


@app.route('/select_book/<int:book_id>')
def select_book(book_id):
    """
    切换当前词书（GET请求）
    <int:book_id> 是 Flask 的路由转换器：只匹配数字，如 /select_book/2。
    流程：确认词书存在 -> 写入 current_book.txt -> 重定向回首页。
    """
    with open_db() as conn:
        row = conn.execute(
            'SELECT id, name FROM books WHERE id = ?', (book_id,)
        ).fetchone()

    if row is None:
        # 词书不存在（比如手动输入了不存在的ID）：提示并回词书管理页
        flash('切换失败：词书不存在', 'error')
        return redirect(url_for('books'))

    save_current_book_id(book_id)

    # 【Bug1修复】切换词书后必须清掉 session 里记住的旧词：
    # 那个词属于旧词书，如果不清，虽然首页的三重校验（状态=new + 属当前词书）
    # 会兜底重抽，但显式清空能保证状态一定干净、不留歧义
    session.pop('current_word_id', None)

    flash(f'已切换到词书「{row["name"]}」，开始学习吧！', 'success')
    return redirect(url_for('index'))


@app.route('/rename_book/<int:book_id>', methods=['POST'])
def rename_book(book_id):
    """
    重命名词书（POST请求）
    前端在词书管理页点击"重命名"按钮 -> prompt() 弹框输入新名字 ->
    JS 动态创建隐藏表单 POST 提交到本路由。
    规则：
    - 新名字为空（或全是空格）-> 不做任何修改，flash 错误提示
    - 词书ID不存在（比如手动输入了不存在的ID）-> flash 错误提示
    - 成功 -> 更新 books 表的 name 字段（最长50字符），重定向回 /books
    注意：current_book.txt 里存的是词书 ID 而不是名字，
    所以重命名完全不影响"当前正在学哪本书"的状态。
    """
    new_name = request.form.get('new_name', '').strip()

    # 空名字：不做修改（需求明确要求"为空则不修改"）
    if not new_name:
        flash('重命名失败：名称不能为空', 'error')
        return redirect(url_for('books'))

    # 【重构说明】原来"词书不存在"的分支要手动 conn.close() 后再 return；
    # 现在 with 块里直接 return，连接自动关闭，不会再漏
    with open_db() as conn:
        row = conn.execute('SELECT id FROM books WHERE id = ?', (book_id,)).fetchone()
        if row is None:
            flash('重命名失败：词书不存在', 'error')
            return redirect(url_for('books'))

        # 只改 books 表的 name 字段，words 表的数据完全不动
        conn.execute('UPDATE books SET name = ? WHERE id = ?', (new_name[:50], book_id))
        conn.commit()

    flash(f'词书已重命名为「{new_name[:50]}」', 'success')
    return redirect(url_for('books'))


@app.route('/delete_book/<int:book_id>', methods=['POST'])
def delete_book(book_id):
    """
    【新增】删除整本词书（POST请求）

    安全限制（两道红线，绝对不能删）：
      1. id=1 的"默认六级词库"：系统的根基，删除后老数据全部失去归属
      2. 4 本 AI 分类词书（AI评估词库-六级/雅思/托福/考研）：
         AI 评估功能的专属存储位置，删了对应类别的评估页就没词可查
      试图删除这些书时，flash 报错"系统词书，无法删除"，数据库一个字节都不动。

    删除逻辑（在一个连接里顺序执行，最后统一 commit）：
      1. DELETE FROM words WHERE book_id = ?  —— 删掉这本书里的所有单词
      2. DELETE FROM books WHERE id = ?       —— 删掉词书记录本身
      【注意】study_log 学习历史表一行都不碰：
             日志里的 word_text 是冗余保存的拼写，即使单词和词书被删，
             /history 页面的历史记录依然完整可查。

    特殊处理：如果删掉的恰好是"当前正在学习的词书"，
    必须把 current_book.txt 强制切回默认词书（id=1），
    否则首页会去加载一个已不存在的词书（虽然 load_current_book_id 有兜底，
    但显式切回语义更清晰，也能立即生效）。

    前端 books.html 已为该按钮加了 confirm 二次确认，这里是最后一道防线。
    """
    with open_db() as conn:
        # ---- 第1步：先查出词书名字（既要判断保护名单，又要用于提示文案）----
        row = conn.execute(
            'SELECT name FROM books WHERE id = ?', (book_id,)
        ).fetchone()
        if row is None:
            # 词书不存在（比如手动构造了一个不存在的ID）
            flash('删除失败：词书不存在', 'error')
            return redirect(url_for('books'))
        book_name = row['name']

        # ---- 第2步：系统词书保护 ----
        # 【改造】默认词库（id=1）和 4 本 AI 分类词书都不允许删除。
        # 用 AI_BOOK_NAMES.values() 精确匹配书名：将来增删考试类别时
        # 只需要改配置字典，这里的保护逻辑自动跟随，不用再改代码。
        if book_id == 1 or book_name in AI_BOOK_NAMES.values():
            flash('系统词书，无法删除', 'error')
            return redirect(url_for('books'))

        # ---- 第3步：先删词、再删书（同一事务内，统一 commit）----
        conn.execute('DELETE FROM words WHERE book_id = ?', (book_id,))
        conn.execute('DELETE FROM books WHERE id = ?', (book_id,))
        conn.commit()
        # 注意：全程没有碰 study_log，历史学习记录完整保留

    # ---- 第4步：删的是当前词书 -> 强制切回默认词书（id=1）----
    if load_current_book_id() == book_id:
        save_current_book_id(1)
        # 顺手清掉 session 里记住的"当前新词"：那个词可能属于刚被删的词书
        session.pop('current_word_id', None)

    flash(f'词书「{book_name}」及里面的单词已成功删除。', 'success')
    return redirect(url_for('books'))


@app.route('/reset_book', methods=['POST'])
def reset_book():
    """
    重置"当前词书"的学习进度（POST请求，配二次确认弹框防误触）
    做三件事：
    1. 把当前词书的所有单词恢复为未学状态：
       status='new'（未学）、review_stage=0（复习阶段清零）、
       两个时间字段清空（这样会重新进入抽词池，可以重新学一遍）
    2. 清理今日新词进度：只删掉进度文件里"当前词书"这一行，
       其他词书今天的进度保留（多词书进度互不影响）
    3. flash 成功提示 + 重定向回首页

    【核心原则】study_log 表（学习记录）一行都不动！
    本路由只 UPDATE words 表和改进度文件，历史记录永远保留。
    """
    # 注意：current_book.txt 里存的是词书ID，重置的是"当前选中"的这本书
    book_id = load_current_book_id()

    with open_db() as conn:
        # 把当前词书的所有单词恢复为未学状态（study_log 历史记录不碰）
        conn.execute(
            '''UPDATE words
               SET status           = 'new',
                   review_stage     = 0,
                   next_review_time = NULL,
                   last_review_time = NULL
               WHERE book_id = ?''',
            (book_id,)
        )
        conn.commit()

    # ---- 清理今日进度文件（格式：每行"日期|词书ID|批次|数量"）----
    # 策略：只移除"今天 + 当前词书"的那一行，其他词书今天的进度原样保留。
    # 前缀里结尾的 '|' 很关键：能防止 book_id=1 误匹配到 book_id=10 的行
    today_str = datetime.now().strftime('%Y-%m-%d')
    prefix = f'{today_str}|{book_id}|'
    if os.path.exists(DAILY_PROGRESS_FILE):
        try:
            with open(DAILY_PROGRESS_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            with open(DAILY_PROGRESS_FILE, 'w', encoding='utf-8') as f:
                f.writelines(ln for ln in lines if not ln.startswith(prefix))
        except OSError:
            # 进度文件读写失败不影响重置本身（大不了今天多/少计几个新词）
            pass

    flash('当前词书的进度已重置，可以重新开始学习了！', 'success')
    return redirect(url_for('index'))


@app.route('/continue_learning', methods=['POST'])
def continue_learning():
    """
    【加练】"继续学习"按钮（POST请求）：
    把当前词书的批次号 +1、当前批次已学数量清零，然后回首页。
    回首页后"今日已学数量 < 每日目标"重新成立，于是会继续抽出新词，
    实现同一天多轮学习。

    进度文件的变化：2026-10-06|1|1|40  ->  2026-10-06|1|2|0
    （第1轮学满40个后点击，进入第2轮，数量归零重新计数）
    """
    book_id = load_current_book_id()
    batch, _ = load_progress_entry(book_id)       # 【死代码清理】count 此处用不到，改为 _
    save_progress_entry(book_id, batch + 1, 0)    # 批次+1，数量清零

    # 加练开启了新一轮，把 session 里记住的"当前新词"清掉，
    # 首页会按新批次重新随机抽词
    session.pop('current_word_id', None)

    flash(f'已开启新一轮学习（第{batch + 1}轮），继续加油！', 'success')
    return redirect(url_for('index'))


@app.route('/history')
def history():
    """
    学习记录页（GET请求）
    逻辑：
    1. 从 study_log 表查出所有日志，SQL 按 study_date 倒序（同一天内按插入顺序）
    2. LEFT JOIN words 表拿释义（单词被删时释义为空，仍能显示拼写）
    3. 在 Python 中把同一天的记录分组，并统计当天总数/认识数/不认识数
    4. 传给 history.html 渲染：按天倒序展示，点击某天可展开单词列表
    """
    with open_db() as conn:
        rows = conn.execute(
            '''SELECT s.study_date,
                      s.batch_num,
                      s.word_text,
                      s.action,
                      w.meaning
               FROM study_log s
               LEFT JOIN words w ON s.word_id = w.id
               ORDER BY s.study_date DESC, s.batch_num DESC, s.id ASC'''
        ).fetchall()

        # 给统一导航栏的"复习（X）"标签提供待复习数量：
        # 只统计"当前词书"中 next_review_time 已到期且 status='learning' 的单词
        # 【重构】SQL 已抽到公共函数 count_review_pending
        book_id, _ = get_current_book(conn)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        review_pending = count_review_pending(conn, now_str, book_id)

    # ---- Python 分组（【加练批次】按"日期+批次"联合分组；连接已关闭，纯内存操作）----
    # 加练功能上线后，同一天可能有多轮学习记录（batch_num=1,2,3...），
    # 分组的"天"变成了 (study_date, batch_num) 二元组，
    # 历史页展示成"2026-10-06 第1次学习 / 2026-10-06 第2次学习"两块
    history_days = []
    day_map = {}   # (日期, 批次) -> 对应那天的数据块
    for row in rows:
        date = row['study_date']
        # 旧记录的 batch_num 可能是 NULL（理论上 ALTER 默认值会填1，这里防御一下）
        batch = row['batch_num'] if row['batch_num'] is not None else 1
        key = (date, batch)
        if key not in day_map:
            # 第一次遇到这个 (日期, 批次) 组合：新建数据块，按顺序追加到结果列表
            entry = {
                'date': date,
                'batch': batch,  # 第几次学习（批次号），模板显示"第X次学习"
                'total': 0,      # 当批学习总次数
                'known': 0,      # 点"认识"的次数
                'unknown': 0,    # 点"不认识"的次数
                'words': [],     # 当批的单词明细
                # 注意：键名用 words 而不是 items！
                # 因为模板里 day.items 会命中字典自带的 .items() 方法，导致报错
            }
            day_map[key] = entry
            history_days.append(entry)

        entry['total'] += 1
        if row['action'] == '认识':
            entry['known'] += 1
        else:
            entry['unknown'] += 1
        entry['words'].append({
            'word': row['word_text'],
            # 单词可能已被删除（LEFT JOIN 查不到），此时 meaning 为 None，模板里会显示占位提示
            'meaning': row['meaning'],
            'action': row['action'],
        })

    # mode='history' 告诉共享导航栏：当前页是学习记录，高亮这个标签
    return render_template('history.html',
                           history_days=history_days,
                           mode='history',
                           review_pending=review_pending)


# ====================================================================
# AI 单词评估路由
# ====================================================================

@app.route('/ai_book')
def ai_book():
    """
    AI 评估词库页面（GET请求）
    逻辑：
    0.【改造】从 URL 读取要查看的考试分类（如 /ai_book?exam_type=雅思），
       不传或传非法值时回退默认"六级"，页面永远有内容可显示
    1. 拿到该分类对应词书（如"AI评估词库-雅思"）的 book_id
    2. 只查出这本书下的全部单词，按 ai_score 从高到低排序（同分按入库时间倒序）
       —— 点哪个分类标签就只显示哪个分类的单词，四本词书互不混放
    3. 顺带统计平均分，给页面做个总览（同样只统计当前分类的词书）
    4. 计算当前学习词书的待复习数量，供统一导航栏的"复习"标签使用
       【注意】这个统计口径与其他页面完全一致，只看"当前学习词书"，
       与 AI 分类词书无关，不会被本页的 exam_type 参数影响
    5. 渲染 ai_book.html
    """
    # ---- 第0步：解析 URL 里的分类参数（GET 参数，不是表单）----
    exam_type = request.args.get('exam_type', DEFAULT_EXAM_TYPE).strip()
    if exam_type not in EXAM_TYPES:
        # 有人手动构造了不存在的分类（如 ?exam_type=高考）：回退默认六级
        exam_type = DEFAULT_EXAM_TYPE

    with open_db() as conn:
        # 只拿当前分类对应的那本 AI 词书
        ai_book_id = get_ai_book_id(conn, exam_type)

        # 查询多带一个 exam_type 字段（六级/雅思/托福/考研），
        # 前端卡片据此显示考试标签。
        # 排序仍按 AI 评分降序：最重要的 5 星词排最前面；同分再按 id 倒序（新评估的排前面）
        word_list = conn.execute(
            '''SELECT id, word, meaning, ai_score, ai_reason, exam_type
               FROM words
               WHERE book_id = ?
               ORDER BY ai_score DESC, id DESC''',
            (ai_book_id,)
        ).fetchall()

        # 平均分（只做展示用，用 round 保留一位小数；词库为空时显示 0）
        avg_row = conn.execute(
            'SELECT AVG(ai_score) AS avg_score, COUNT(*) AS total FROM words WHERE book_id = ?',
            (ai_book_id,)
        ).fetchone()
        avg_score = round(avg_row['avg_score'], 1) if avg_row['avg_score'] is not None else 0
        total_count = avg_row['total']

        # 导航栏"复习(X)"角标需要待复习数量，统计口径和其他页面完全一致：
        # 只统计"当前正在学习的词书"（不是 AI 词书）中到期且 status='learning' 的词
        # 【重构】SQL 已抽到公共函数 count_review_pending
        current_book_id, _ = get_current_book(conn)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        review_pending = count_review_pending(conn, now_str, current_book_id)

    # mode='ai' 告诉共享导航栏：当前在 AI 评估页，高亮"AI评估"标签
    # exam_types 传给模板：上方分类标签页 + 输入区的评估按钮都根据这份清单循环生成
    # exam_type 传给模板：当前正在查看的分类，用于标签页高亮和文案显示
    return render_template('ai_book.html',
                           word_list=word_list,
                           avg_score=avg_score,
                           total_count=total_count,
                           exam_types=EXAM_TYPES,
                           exam_type=exam_type,
                           mode='ai',
                           review_pending=review_pending)


@app.route('/save_api_key', methods=['POST'])
def save_api_key():
    """
    【新增】保存 AI 平台密钥（AI评估页"AI 配置"卡片提交）。
    三方协调一致的关键：内存全局变量 API_KEY、config.txt 文件、
    load_api_key()（启动时读文件）——这里同时更新前两者，
    做到"保存后立即生效，无需重启程序"。
    """
    global API_KEY
    # strip() 去掉粘贴时可能带入的首尾空格/换行
    new_key = request.form.get('api_key', '').strip()

    if new_key:
        # 正常保存：更新内存变量 + 覆盖写入 config.txt
        API_KEY = new_key
        msg, category = 'API Key 已保存，无需重启即可生效', 'success'
    else:
        # 留空提交 = 用户想清空密钥：恢复为占位符，下次 AI 评估会被拦截并提示配置
        API_KEY = API_KEY_PLACEHOLDER
        msg, category = 'API Key 已清空', 'error'

    # 统一把内存中的当前值写回 config.txt（保存的是新 Key 或占位符）
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write(API_KEY)

    flash(msg, category)
    return redirect(url_for('ai_book'))


@app.route('/reset_all_data', methods=['POST'])
def reset_all_data():
    """
    【新增】一键重置所有数据（恢复出厂设置）。
    危险操作：删除整个 vocab.db（词书、学习进度、AI 评估记录全部清空），
    然后重建表结构并自动导入默认词书。
    """
    # ================================================================
    # 关键：删除数据库文件前，必须保证没有任何活动连接占着文件！
    # Windows 上 SQLite 的机制是：只要有一个连接没 close，文件就被锁定，
    # os.remove() 会直接抛 PermissionError 删不掉。
    # 安全性依据（三层）：
    #   1. 本函数全程【不调用】open_db()/get_db_connection()，不新开连接；
    #   2. 其他请求里的连接都由 open_db() 的 with/finally 结构保证已 close；
    #   3. 万一仍有其他线程的连接恰好持有文件（极端并发），os.remove 抛出
    #      PermissionError，被下面的 except 捕获，给出友好提示而不是 500 崩溃页。
    # ================================================================
    try:
        # 三件套一起删：主库 + WAL 衍生文件（-wal 未合并日志 / -shm 共享内存索引）。
        # 只删主库不删 WAL 的话，残留的 -wal 里可能还躺着旧数据，
        # 重建后一旦触发 checkpoint，被删掉的记录会被"复活"
        for suffix in ('', '-wal', '-shm'):
            file_path = DB_PATH + suffix
            if os.path.exists(file_path):
                os.remove(file_path)

        # 数据库之外的两个进度小文件一并清掉（都有"文件不存在"兜底，删了也安全）：
        #   DAILY_PROGRESS_FILE：今日新词进度（日期|词书ID|批次），不清会残留指向旧词书 ID 的进度
        #   CURRENT_BOOK_FILE ：当前选中的词书 ID，删掉后 load_current_book_id()
        #                       读不到文件会自动回退到默认词书 1
        for txt_file in (DAILY_PROGRESS_FILE, CURRENT_BOOK_FILE):
            if os.path.exists(txt_file):
                os.remove(txt_file)
    except PermissionError:
        flash('重置失败：数据库文件正被占用，请关闭本程序的其他窗口后重试', 'error')
        return redirect(url_for('index'))

    # session 里可能缓存着指向旧单词的 current_word_id，一并清掉
    session.pop('current_word_id', None)

    # ---- 删干净后立即重建：建表 + 补齐 4 本 AI 分类词书 + 自动导入默认词库 ----
    init_database()

    flash('所有数据已重置，已恢复出厂设置', 'success')
    return redirect(url_for('index'))


@app.route('/ai_evaluate', methods=['POST'])
def ai_evaluate():
    """
    AI 评估入库（POST请求）—— 多考试类型版，完整流程：
      1. 取表单里的单词和考试类型（exam_type 由前端按钮对应的隐藏字段提交）
      2. 校验：单词不能为空/不能是中文；考试类型必须在 EXAM_TYPES 白名单里
      3. 查重维度升级为 (word + exam_type)：
         同一个"单词+考试"组合不允许重复；但同一个单词按不同考试分别评估，
         会插入各自独立的新记录（如 abandon 的六级版和雅思版可以共存）
      4. 调用 get_ai_evaluation(word, exam_type) 让对应考试的 AI 专家打分
      5. 直接 INSERT 一条完整记录（单词/释义/评分/理由/考试类型/词书ID）。
         注意：这里【不能】复用第二版的 merge_or_insert_word()，
         因为它只按 (word, book_id) 判重，会把"雅思版abandon"错误合并进
         "六级版abandon"，所以多考试场景必须用专属的插入逻辑。
      6. 成功/失败全部 flash 提示，最后重定向回 /ai_book（POST-重定向-GET 模式）
    """
    # ---- 第1步：取单词和考试类型 ----
    word = request.form.get('word', '').strip()
    word = word.lower()          # 统一小写，避免 "Apple" 和 "apple" 被当成两个词
    exam_type = request.form.get('exam_type', DEFAULT_EXAM_TYPE).strip()

    # ---- 第2步：输入校验 ----
    if not word:
        flash('评估失败：请输入要评估的英文单词', 'error')
        return redirect(url_for('ai_book', exam_type=exam_type))
    # 复用 has_chinese()：输入里含中文汉字肯定不是英文单词，直接拦下
    if has_chinese(word):
        flash('评估失败：请输入英文单词，不要包含中文', 'error')
        return redirect(url_for('ai_book', exam_type=exam_type))
    # 考试类型白名单校验：防止有人绕过页面手动提交一个不存在的考试名
    if exam_type not in EXAM_TYPES:
        flash('评估失败：不支持的考试类型', 'error')
        return redirect(url_for('ai_book', exam_type=exam_type))
    # 【新增】API Key 前置检查：密钥未配置（占位符/为空）时直接提示，
    # 不再发出一个注定 401 的请求，错误信息也更友好
    if not is_api_key_valid():
        flash('AI 评估失败：请先在 config.txt 中配置有效的 API Key', 'error')
        return redirect(url_for('ai_book', exam_type=exam_type))

    # 【核心重构②】原来每个错误分支都要手写 conn.close() 再 return；
    # 现在统一放进 with 块，任何一处 return（重复词/网络失败/解析失败/写库失败）
    # 连接都会自动关闭，既不会漏关也不会重复写
    with open_db() as conn:
        # 【改造】按本次提交的考试类型拿到对应分类词书的 book_id：
        # 六级的词进"AI评估词库-六级"、雅思的词进"AI评估词库-雅思"……各回各家
        ai_book_id = get_ai_book_id(conn, exam_type)

        # ---- 第3步：按 (word + exam_type) 二维查重 ----
        # 关键区别：同一单词在六级库已存在，不影响再评一次雅思；
        # 只有"单词和考试类型完全相同"时才判定为重复
        existing = conn.execute(
            'SELECT id FROM words WHERE word = ? AND book_id = ? AND exam_type = ?',
            (word, ai_book_id, exam_type)
        ).fetchone()
        if existing is not None:
            # 需求规定的报错文案，带上具体考试类型
            flash(f'该单词已在{exam_type}词库中', 'error')
            return redirect(url_for('ai_book', exam_type=exam_type))

        # ---- 第4步：调用 AI 评估（传入考试类型，使用对应的动态专家人设）----
        # 网络异常、密钥错误、超时、JSON 解析失败等所有意外都在这里统一兜底，
        # 保证错误时页面不会崩，而是带着友好提示返回 AI 词库页
        try:
            result = get_ai_evaluation(word, exam_type)
        except requests.exceptions.RequestException as e:
            flash(f'AI 评估失败：网络请求出错（{e}），请检查网络或 API_KEY 后重试', 'error')
            return redirect(url_for('ai_book', exam_type=exam_type))
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            flash(f'AI 评估失败：返回内容无法解析（{e}），请重试一次', 'error')
            return redirect(url_for('ai_book', exam_type=exam_type))

        # ---- 第5步：插入一条完整的新记录 ----
        # 一条 INSERT 同时写入：单词、释义、所属AI词书、AI评分、评估理由、考试类型。
        # status 用默认值 'new'，与之前评估入库的单词行为保持一致。
        try:
            conn.execute(
                '''INSERT INTO words
                       (word, phonetic, meaning, book_id, ai_score, ai_reason, exam_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (word, '', result['meaning'], ai_book_id,
                 result['score'], result['reason'], exam_type)
            )
            conn.commit()
        except sqlite3.Error:
            # 数据库异常：回滚，避免留下写了一半的数据
            conn.rollback()
            flash('AI 评估失败：数据库写入出错，请重试', 'error')
            return redirect(url_for('ai_book', exam_type=exam_type))

    # ---- 第6步：成功提示（带考试类型 + ⭐ 个数 = AI 评分）----
    stars = '⭐' * result['score']
    flash(
        f'[{exam_type}]评估成功：{word} {result["meaning"]} '
        f'评分 {result["score"]}/5 {stars}（{result["reason"]}）',
        'success'
    )
    return redirect(url_for('ai_book', exam_type=exam_type))


@app.route('/delete_ai_word/<int:word_id>', methods=['POST'])
def delete_ai_word(word_id):
    """
    删除 AI 评估词库中的某一个单词（POST请求）

    路由参数：
      <int:word_id> 是 Flask 的整型转换器，只匹配数字（如 /delete_ai_word/12），
      非数字路径（如 /delete_ai_word/abc）会直接返回 404，天然挡掉非法输入。

    【改造】安全要点（4 本 AI 词书并存后的新写法）：
      1. 不再固定用一个 ai_book_id 做条件，而是先联表查出这个单词
         属于哪本词书（words JOIN books），再判断该书名是否是
         AI_BOOK_NAMES 里 4 本分类词书之一：
         - 属于任意一本 AI 分类词书 -> 允许删除（无需知道具体类别，
           单词自己身上的 book_id 就是"它属于哪本书"的权威答案）
         - 不属于（默认词库/自定义词书的单词）-> 拒绝删除（越权防护）
      2. 只 DELETE words 表这一条记录；study_log 学习日志表完全不碰，
         所以"学习记录"页的历史永远保留（需求明确要求）。
      3. 用 cursor.rowcount 判断是否真的删到了行：
         删到 0 行 = 这个 ID 不存在、或它不属于 AI 词书，给出错误提示。

    流程：查出单词拼写和所属词书 -> 校验归属并删除 -> flash 提示 -> 重定向
    """
    with open_db() as conn:
        # 联表一次拿到：单词拼写（flash 用）、book_id（删除条件用）、
        # 词书名（归属校验用）、单词的考试类型（删除后跳回对应分类页用）
        row = conn.execute(
            '''SELECT w.word, w.book_id, w.exam_type, b.name AS book_name
               FROM words w JOIN books b ON b.id = w.book_id
               WHERE w.id = ?''',
            (word_id,)
        ).fetchone()

        # 归属校验：书名必须精确命中 4 本 AI 分类词书之一才允许删。
        # 手动构造默认词库/自定义词书单词的 ID 提交过来，在这里被拦下。
        if row is not None and row['book_name'] in AI_BOOK_NAMES.values():
            word_text = row['word']
            # WHERE 同时限定 id 和 book_id，双条件保证只删目标词书里的目标词
            cursor = conn.execute(
                'DELETE FROM words WHERE id = ? AND book_id = ?',
                (word_id, row['book_id'])
            )
            conn.commit()
            # 【重构说明】把 rowcount 先存成普通变量：
            # 离开 with 块连接就关闭了，逻辑上不再依赖 cursor 对象
            deleted_count = cursor.rowcount
        else:
            # 单词不存在，或它不属于任何 AI 分类词书（越权删除被拦住）
            deleted_count = 0
            word_text = row['word'] if row is not None else '未知单词'

    if deleted_count == 0:
        # 没删到任何行：单词不存在，或它不属于 AI 评估词库
        flash('删除失败：该单词不存在，或不属于 AI 评估词库', 'error')
    else:
        # 成功提示带上刚删除的单词拼写，用户一眼知道删掉的是哪个词
        flash(f'单词 "{word_text}" 已成功删除', 'success')

    # 删除后跳回被删单词所属的分类页（exam_type 为空的老数据回退六级），
    # 保证用户停留在原来的分类标签上；POST -> 重定向 -> GET 也避免重复提交
    back_type = row['exam_type'] if (row is not None and row['exam_type'] in EXAM_TYPES) \
        else DEFAULT_EXAM_TYPE
    return redirect(url_for('ai_book', exam_type=back_type))


# ====================================================================
# 启动程序
# ====================================================================

if __name__ == '__main__':
    print('=' * 50)
    print('六级背单词 - 第三版（AI词汇评估系统）启动')
    print('=' * 50)
    init_database()
    print(f'每日新词目标：{load_daily_goal()} 个')
    # 启动时提醒检查 API_KEY 是否已配置（密钥现在从 config.txt 读取）。
    # 说明：这不是死代码——config.txt 里是占位符或为空时就会触发提醒。
    if not is_api_key_valid():
        print('⚠️  警告：API_KEY 未配置！请在 config.txt 中填入真实密钥（sk- 开头），否则AI评估不可用！')
    print('访问 http://127.0.0.1:5000 开始学习')
    print('复习模式：http://127.0.0.1:5000/review')
    print('AI 评估：http://127.0.0.1:5000/ai_book')
    print('=' * 50)
    app.run(debug=True, port=5000)
