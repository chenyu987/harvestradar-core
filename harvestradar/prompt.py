"""Extraction prompt and output schema.

The prompt is versioned: bump PROMPT_VERSION on every change so eval runs
can be compared across versions.
"""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
你在帮一家物品租借中介看群聊截图，找出群里「想租 / 借 / 求」物品的人。

背景：
- 类型是 Alpha 和 Beta 两种物品，每件物品登记了使用人，所以使用人的性别很重要。
- 群里常见几类消息，只有第一类是我们要的：
  1. request：有人想租、借、求一件去用（「求」「有人出…吗」「可以借一下」「想租」）。
  2. offer：物主在出货、出租（「出女beta」「出alpha」「有货可租」）。这是供给，不是需求。
  3. carpool：拼车、找车、找人同行（「车找人」「人找车」）。虽然提到地点和日期，但不是要货。
  4. other：闲聊、换汇（「出rmb」「需要美金」）、广告、入群提示、系统消息。
- 一条消息可能同时出货又求货，按它的主要意图归类；真有两个意图就拆成两条。

对截图里每一条跟物品、拼车有关的消息输出一条记录（other 类可以不输出）。
字段规则：
- sender：消息上方显示的发言人名字，原样照抄（含 emoji 和繁体），不要翻译。
- kind：request / offer / carpool / other。
- item_type：alpha / beta / unknown（没说就是 unknown，不要猜）。
- location：地点的英文名，小写，比如 lakeside、hillcrest、pinewood；常见缩写要展开（lks → lakeside）。没说就留空字符串。
- dates：使用日期，格式 MM-DD 的列表，一天一个；「3/14-3/16」展开成三天。没说就空列表。
- male / female：要几件男款、几件女款。「男生」= 1 男；说了「2张」但没说性别就都填 0，在 notes 里写清楚。
- quote：消息原文，原样照抄。
- sent_at：这条消息上方最近的时间分隔线原文（如「3/14 11:44」「Yesterday 10:02」），看不到就留空字符串。
- notes：看不清、有歧义、或者你不确定的地方写在这里，否则留空字符串。

只根据截图里看得到的内容填，看不清的字不要编。截图里一条相关消息都没有，就返回空列表。
"""

LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "leads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sender": {"type": "string"},
                    "kind": {"type": "string", "enum": ["request", "offer", "carpool", "other"]},
                    "item_type": {"type": "string", "enum": ["alpha", "beta", "unknown"]},
                    "location": {"type": "string"},
                    "dates": {"type": "array", "items": {"type": "string"}},
                    "male": {"type": "integer"},
                    "female": {"type": "integer"},
                    "quote": {"type": "string"},
                    "sent_at": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "sender", "kind", "item_type", "location", "dates",
                    "male", "female", "quote", "sent_at", "notes",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["leads"],
    "additionalProperties": False,
}
