# ═══════════════════════════════════════════════════════════════
# 高考志愿底线澄清助手 — Agent 后端
# 基于 c5_guard.py 架构改造，按 T4 PRD + T5 设计卡实现
# ═══════════════════════════════════════════════════════════════
from dotenv import load_dotenv
from openai import OpenAI
import os, json, sys, re, hashlib

if sys.platform == "win32":
    import io
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

# ── API 客户端 ──────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 优先从 .env 加载，没有则用系统环境变量（Railway 等平台）
_dotenv_path = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_dotenv_path):
    load_dotenv(_dotenv_path)
else:
    load_dotenv()  # 从当前目录或系统查找

API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# 默认用 deepseek-chat（V3），更稳定
MODEL    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

if not API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY 环境变量未设置。请在 Railway Variables 或 .env 中配置。")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ═══════════════════════════════════════════════════════════════
# 工具层：safety_check + 记忆 + 清单/话术生成
# ═══════════════════════════════════════════════════════════════

# ── 危机关键词检测（本地的第一道闸，不依赖 LLM）────────────
CRISIS_RED = [
    "不想活了", "活着没意思", "想结束", "死了算了", "自杀",
    "自伤", "自残", "割腕", "跳楼", "安眠药",
    "想死", "结束生命", "不想继续", "没意义活着",
    # 迭代第1轮补充：隐晦被动自杀表述（C3 FAIL 修复）
    "不要醒来", "不再醒来", "睡过去别醒", "睡着一觉不醒",
    "消失了就好", "不存在就好", "消失了算了",
]
CRISIS_YELLOW = [
    "算了,听他们的吧", "说了也没用", "我好累", "我是不是有问题",
    "我什么都想不了", "我好没用", "没人理解我", "撑不下去了",
    # 迭代第1轮补充：隐晦自我否定/绝望信号（C4 FAIL 修复）
    "意见不重要", "什么都一样", "无所谓的", "无所谓了",
    "没人听我的", "说了也没人听", "没有人会在乎",
    # 迭代第2轮补充：认知混乱/崩溃信号（C9 改进）
    "快要疯了", "快疯了", "什么都不知道", "脑子乱了",
]

# 迭代第2轮新增：家庭风险检测（C10 FAIL 修复）
FAMILY_RISK_KEYWORDS = ["滚出去", "赶我走", "打我", "揍我", "家暴", "威胁我"]
FAMILY_MEMBER_KEYWORDS = ["爸", "妈", "父", "母", "家里", "家人"]

CRISIS_HOTLINES = [
    "北京心理援助热线: 010-82951332",
    "全国青少年服务热线: 12355",
    "全国心理援助热线: 400-161-9995",
]

def safety_check(user_input: str) -> dict:
    """本地关键词 + 规则危机检测。返回灯色、判定理由、建议动作。"""
    text = user_input.lower()
    for kw in CRISIS_RED:
        if kw in text:
            return {
                "light": "red",
                "reason": f"检测到危机关键词「{kw}」",
                "action": "immediate_redirect",
                "message": (
                    "我听到你说了一些让我很担心的话。我不是心理咨询师，"
                    "但你的感受很重要。请拨打以下 24 小时热线，"
                    "也请告诉你信任的成年人或学校心理老师：\n"
                    + "\n".join(CRISIS_HOTLINES) +
                    "\n\n我们先不谈志愿的事了，好吗？"
                ),
            }
    # 迭代第2轮新增：家庭风险检测（C10 FAIL 修复）
    has_family_risk = any(kw in text for kw in FAMILY_RISK_KEYWORDS)
    has_family_member = any(kw in text for kw in FAMILY_MEMBER_KEYWORDS)
    if has_family_risk and has_family_member:
        return {
            "light": "yellow",
            "reason": "检测到家庭风险信号（家庭成员+威胁/暴力）",
            "action": "slow_down",
            "message": (
                "你说的这个情况让我有点担心。如果你在家里感到不安全，"
                "可以联系你信任的老师、亲戚，或者拨打全国青少年服务热线 12355。"
                "你的安全比志愿重要。"
            ),
        }
    for kw in CRISIS_YELLOW:
        if kw in text:
            return {
                "light": "yellow",
                "reason": f"检测到情绪低落信号「{kw}」",
                "action": "slow_down",
                "message": (
                    "听起来你现在挺累的。要不我们先停一停？"
                    "如果你觉得很难受，可以拨打全国青少年服务热线 12355 聊聊。"
                ),
            }
    return {"light": "green", "reason": "正常内容", "action": "continue"}


# ── 对话记忆（文件化，化名存储）───────────────────────────
MEMORY_DIR = os.path.join(_SCRIPT_DIR, "memory")
os.makedirs(MEMORY_DIR, exist_ok=True)

def _session_path(session_id: str) -> str:
    safe = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return os.path.join(MEMORY_DIR, f"{safe}.json")

def memory_read(session_id: str) -> dict:
    """读情景记忆：当前阶段、已澄清底线、聊到第几个问题、情绪状态。"""
    p = _session_path(session_id)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "stage": "opening",
        "clarified": [],
        "accepted": [],
        "rejected": [],
        "risk_accepted": [],
        "question_index": 0,
        "emotion_state": "neutral",
        "summary": "",
    }

def memory_write(session_id: str, data: dict):
    """写情景记忆。"""
    p = _session_path(session_id)
    m = memory_read(session_id)
    m.update(data)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


# ── 价值澄清问题库（语义记忆 — T5 定义）────────────────────
CLARIFY_QUESTIONS = [
    # 维度1: 学习内容偏好
    {"dim": "content", "q": "有没有哪类课，你一想到要学四年就觉得有点抗拒？", "type": "reject"},
    {"dim": "content", "q": "高中阶段，有没有哪门课虽然成绩还行，但你其实不太喜欢？", "type": "reject"},
    {"dim": "content", "q": "你觉得自己更喜欢动手实践、理论推导、还是和人打交道？", "type": "prefer"},
    # 维度2: 工作节奏
    {"dim": "pace", "q": "你想象未来的工作节奏——更希望稳定规律，还是接受不固定的高强度？", "type": "prefer"},
    {"dim": "pace", "q": "你能接受毕业后还需要长期加班或持续考证的工作吗？", "type": "reject"},
    # 维度3: 城市/地理
    {"dim": "location", "q": "有没有你完全不想去的城市？为什么？", "type": "reject"},
    {"dim": "location", "q": "你愿意为一份好工作/好学校离开家乡多远？", "type": "prefer"},
    # 维度4: 风险承受
    {"dim": "risk", "q": "如果一个专业就业前景不太明朗但你很感兴趣，你愿意赌一把吗？", "type": "prefer"},
    {"dim": "risk", "q": "你更在意「稳」还是「有意思」？", "type": "prefer"},
    # 维度5: 社交方式
    {"dim": "social", "q": "你更喜欢独立完成任务还是团队合作？", "type": "prefer"},
    # 维度6: 成就来源
    {"dim": "achievement", "q": "什么让你觉得「做成了」？是看到具体成果、帮到别人、还是被认可？", "type": "prefer"},
    {"dim": "achievement", "q": "有没有什么是你绝对不能接受的——比如必须说谎、做你不认同的事？", "type": "reject"},
]


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT — 按 T5 人设三段式 + 工作流四件套
# ═══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """你是「志愿决策前的自我澄清陪伴者」。

【身份】
陪伴 18 岁左右的高考生在填报志愿前厘清自己的底线与表达。
你不是升学规划师、心理咨询师，也不是家长的对立面。
你不替用户做决定，不直接推荐学校/专业。

【语气】
温和、简短、不评判。先接住「我不知道」，再把模糊感受变成具体可回答的问题。
- 正例：「不知道自己想要什么，这本身就很正常。我们可以先从「你一想到要读四年就有点抗拒的东西」聊起。」
- 反例：「你都 18 岁了，怎么连自己喜不喜欢都不知道？」

【边界（写死）】
1. 不直接推荐学校/专业，不做录取概率保证
2. 不在用户与家长的冲突中站队，不教唆对抗
3. 不处理心理危机——识别到自伤/自杀/暴力/严重精神困扰信号时，立即停止志愿话题

【工作流】
每轮对话按以下顺序：
1. 读上一轮的场景状态（已澄清了什么、问到第几个问题、情绪状态）
2. 先过 safety_check（由外部执行，你会收到检查结果）
3. 决定本轮处于哪个阶段并回应用户
4. 自检是否越界或是否完成当前阶段目标

【六个阶段】
- opening:  开场接情绪、安全声明、解释我能/不能做什么
- clarify:  逐轮问一个问题维度（内容/节奏/城市/风险/社交/成就），每次只问一个
- confirm:  用户表达了足够多后，生成「底线清单」让用户确认
- script:   确认后生成温和沟通话术，供用户与家人讨论时使用
- closing:  温柔收尾，提醒「最终决策需与真人共同做出」
- crisis:   安全检测触发红灯时，立即停止志愿话题，给危机资源和转介建议

【输出格式】
每轮只输出一个 JSON，不加 markdown 包裹，不加多余文字。
字符串内的双引号用「」替代。

{"stage": "当前阶段", "reply": "给用户的回复", "action": "next_question|summarize|generate_script|close|crisis_redirect"}

当 action 为 "summarize" 时额外带：
{"stage": "confirm", "reply": "...", "action": "summarize", "summary": {"绝对不接受": [...], "可商量": [...], "完全接受": [...], "愿意承受的风险": [...]}}

当 action 为 "generate_script" 时额外带：
{"stage": "script", "reply": "...", "action": "generate_script", "script": "一段可念给家人的温和表达话术"}

如果上一条 safety_check 返回红灯，你必须把 stage 设成 "crisis"，reply 是危机转介内容，action 是 "crisis_redirect"。

追问策略：
- 每次只问一个问题维度，不要一口气问多个
- 用户说「不知道」→ 换角度：「那我们换个问法...」
- 用户回答模糊 → 帮助具体化：「能不能举一个你一想到就觉得'还是算了'的例子？」
- 用户说了足够多(≥3个维度有明确回答) → action 改成 "summarize"
- 用户确认清单后 → action 改成 "generate_script"
- 用户拿到话术后不希望继续 → action 改成 "close"
- safety_check 红灯 → 无条件进入 crisis 阶段，不给志愿建议"""


# ── 历史压缩（来自 c4_compact.py 的逻辑）───────────────────
LIMIT = 16

def compact(messages: list) -> list:
    system = messages[0]
    body = "\n".join(f'{m["role"]}: {m["content"]}' for m in messages[1:])
    summary = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": f"用要点总结这段对话的进展和关键结论，供接力：\n{body}"}],
    ).choices[0].message.content
    print("[压缩] 历史已折叠成一条摘要")
    return [system, {"role": "user", "content": f"【之前进展摘要】\n{summary}"}]


# ── JSON 解析（来自 c5 的 parse，容错处理）────────────────
def parse(s: str) -> dict:
    s = s.strip().strip("`").removeprefix("json").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("回复里没有找到合法 JSON 对象")
    return json.loads(s[start:end + 1])


# ═══════════════════════════════════════════════════════════════
# 对话引擎 — 核心循环
# ═══════════════════════════════════════════════════════════════

class GaokaoAssistant:
    """高考志愿底线澄清助手。每个 session_id 独立。"""

    def __init__(self):
        self.sessions = {}

    def _get_or_create(self, session_id: str) -> list:
        if session_id not in self.sessions:
            self.sessions[session_id] = [
                {"role": "system", "content": SYSTEM_PROMPT}
            ]
        return self.sessions[session_id]

    def _safety_review(self, reply_text: str, user_input: str) -> dict:
        """c3 子 agent 安全复核：独立审「安不安全」。
        用独立 prompt 让模型判断这条回复是否安全、是否越界。"""
        review_prompt = (
            "你是安全审计员。请审查以下对话，判断助手的回复是否安全。\n\n"
            f"【用户输入】{user_input}\n"
            f"【助手回复】{reply_text}\n\n"
            "请只输出一个 JSON："
            '{"safe": true/false, "reason": "一句话说明"}'
            "\n\n判定标准：\n"
            "- 回复是否涉及推荐具体学校/专业 → unsafe\n"
            "- 回复是否站队（偏袒学生或家长）→ unsafe\n"
            "- 回复是否忽视或淡化用户表达的危机信号 → unsafe\n"
            "- 回复是否自称心理咨询师/规划师等越界身份 → unsafe\n"
            "- 其他情况 → safe"
        )
        try:
            r = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": review_prompt}],
                temperature=0.1,
            ).choices[0].message.content
            result = parse(r)
            return {"safe": result.get("safe", True), "reason": result.get("reason", "")}
        except Exception:
            return {"safe": True, "reason": "复核调用失败，放行"}

    def _exit_safety_check(self, reply_text: str) -> dict:
        """出口安全扫描：对 LLM 回复内容再做一次关键词检测。
        如果 LLM 不听指令回了危险内容，这是最后一道闸。"""
        # 合并 entry 和 reply 做检测（看 LLM 有没有忽略红灯）
        combined = reply_text
        for kw in CRISIS_RED:
            if kw in combined:
                return {
                    "light": "red",
                    "reason": f"出口检测到危险关键词「{kw}」",
                    "override": CRISIS_HOTLINES[0],
                }
        return {"light": "green", "reason": "出口安全", "override": None}

    def chat(self, session_id: str, user_input: str) -> dict:
        """处理一轮对话，返回 {"reply": ..., "stage": ..., "safety": ...}。"""
        messages = self._get_or_create(session_id)

        # 1. 入口安全检测（本地关键词，确定性代码）
        safety = safety_check(user_input)
        safety_msg = (
            f"[safety_check 结果] 灯色={safety['light']}，动作={safety['action']}。"
            f"{'请按要求回复危机转介内容，stage=crisis, action=crisis_redirect。' if safety['light'] == 'red' else ''}"
            f"{'请放慢节奏，先确认情绪。' if safety['light'] == 'yellow' else ''}"
        )

        # 2. 读记忆
        mem = memory_read(session_id)
        mem_text = (
            f"[当前记忆] 阶段={mem['stage']}，已问第{mem['question_index']}个问题，"
            f"已澄清: {mem['clarified'][-3:] if mem['clarified'] else '无'}，"
            f"绝对不能: {mem['rejected'][-3:] if mem['rejected'] else '无'}，"
            f"情绪状态: {mem['emotion_state']}"
        )

        # 3. 组装 user 消息
        user_msg = f"{safety_msg}\n{mem_text}\n[用户消息] {user_input}"
        messages.append({"role": "user", "content": user_msg})

        # 4. 调用 LLM（包裹 try/except，失败时返回 FALLBACK）
        try:
            for attempt in range(3):
                if len(messages) > LIMIT:
                    messages = compact(messages)
                    self.sessions[session_id] = messages

                reply = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    temperature=0.7,
                ).choices[0].message.content

                messages.append({"role": "assistant", "content": reply})
                try:
                    action = parse(reply)
                    break
                except Exception:
                    if attempt < 2:
                        messages.append({
                            "role": "user",
                            "content": "上一条不是合法 JSON，请只回一个 JSON，不要 markdown，不要多余文字。"
                        })
                    else:
                        return {
                            "reply": "抱歉，出了一点问题，请再试一次？",
                            "stage": "error",
                            "safety": safety["light"],
                        }
        except Exception:
            # 模型完全不可用时返回 FALLBACK 兜底话术
            return {
                "reply": (
                    "抱歉，服务暂时不太稳定，没能处理你的消息。"
                    "如果你感到着急或不安，可以随时拨打心理援助热线：\n"
                    + "\n".join(CRISIS_HOTLINES) +
                    "\n\n请稍后再试，或刷新页面开始新对话。"
                ),
                "stage": "error",
                "safety": safety["light"],
            }

        # 5. 出口安全扫描（第二道闸，确定性代码）
        exit_safety = self._exit_safety_check(action.get("reply", ""))
        if exit_safety["override"]:
            action["reply"] = (
                "我注意到刚才的回复中可能包含了不安全的信号。"
                "请拨打 24 小时心理援助热线寻求专业帮助：\n"
                + exit_safety["override"] +
                "\n\n我们先不谈志愿的事了。"
            )
            action["stage"] = "crisis"
            action["action"] = "crisis_redirect"
            safety = {"light": "red", "reason": exit_safety["reason"], "action": "immediate_redirect"}

        # 6. c3 子 agent 安全复核（第三道闸，独立模型审查）
        review = self._safety_review(action.get("reply", ""), user_input)
        if not review.get("safe", True):
            action["reply"] = (
                "抱歉，我刚才的回复可能不太合适。请让我重新表达："
                "我不能替你做出志愿选择，但我可以继续陪你厘清自己的想法。"
            )
            safety = {"light": safety["light"], "reason": f"安全复核不通过: {review.get('reason', '')}", "action": "slow_down"}

        # 7. 更新记忆
        self._update_memory(session_id, action, user_input)

        # 8. 如果生成了清单或话术，写入记忆
        if action.get("action") == "summarize" and "summary" in action:
            mem = memory_read(session_id)
            mem["summary_data"] = action["summary"]
            memory_write(session_id, mem)

        return {
            "reply": action.get("reply", ""),
            "stage": action.get("stage", "unknown"),
            "safety": safety["light"],
            "summary": action.get("summary"),
            "script": action.get("script"),
            "action": action.get("action"),
        }

    def _update_memory(self, session_id: str, action: dict, user_input: str):
        mem = memory_read(session_id)
        stage = action.get("stage", mem["stage"])
        mem["stage"] = stage

        # 从用户输入中提取可能的偏好关键词（简化版）
        for kw in ["不喜欢", "讨厌", "抗拒", "不能接受", "绝不"]:
            if kw in user_input:
                mem["rejected"].append(user_input[:60])

        for kw in ["喜欢", "想", "愿意", "可以接受", "感兴趣"]:
            if kw in user_input:
                mem["clarified"].append(user_input[:60])

        if action.get("action") == "next_question":
            mem["question_index"] = mem.get("question_index", 0) + 1

        if action.get("action") in ("summarize", "generate_script", "close"):
            mem["stage"] = stage

        memory_write(session_id, mem)


# ═══════════════════════════════════════════════════════════════
# CLI 调试入口（直接 python gaokao_assistant.py 测试）
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    assistant = GaokaoAssistant()
    print("=" * 50)
    print("高考志愿底线澄清助手 - CLI 测试模式")
    print("输入 'exit' 退出，输入 'reset' 重置对话")
    print("=" * 50)

    sid = "cli-test"
    while True:
        ui = input("\n你：").strip()
        if ui.lower() == "exit":
            break
        if ui.lower() == "reset":
            assistant.sessions.pop(sid, None)
            memory_write(sid, {
                "stage": "opening", "clarified": [], "accepted": [],
                "rejected": [], "risk_accepted": [], "question_index": 0,
                "emotion_state": "neutral", "summary": "",
            })
            print("[已重置]")
            continue

        result = assistant.chat(sid, ui)
        print(f"\n[{result['stage']}·{result['safety']}灯] {result['reply']}")
        if result.get("summary"):
            print(f"\n📋 底线清单：{json.dumps(result['summary'], ensure_ascii=False)}")
        if result.get("script"):
            print(f"\n💬 沟通话术：{result['script']}")

# MIT License | 郑先隽，北师大心理学部教授，人本AI设计与创新
