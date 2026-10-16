import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
plt.rcParams["font.family"] = "DejaVu Sans"

def draw(path, parts, items, title=None, W=13):
    n = len(parts); xs = [1.3 + i * (W - 2.6) / (n - 1) for i in range(n)]
    H = 1.3 + 0.52 * len(items); fig, ax = plt.subplots(figsize=(W, H)); ax.set_xlim(0, W); ax.set_ylim(-H, 0.2); ax.axis("off")
    for x, p in zip(xs, parts):
        ax.add_patch(Rectangle((x - 1.15, -0.55), 2.3, 0.5, fc="#dae8fc", ec="k", lw=1.2))
        ax.text(x, -0.3, p, ha="center", va="center", fontsize=7.8, weight="bold")
        ax.plot([x, x], [-0.55, -H + 0.1], color="gray", lw=1, ls="--")
    y = -0.95
    for it in items:
        k = it[0]
        if k == "m":
            _, a, b, text, dashed, color = (list(it) + [False, "k"])[:6] if len(it) < 6 else it
            xa, xb = xs[a], xs[b]
            if a == b:
                ax.plot([xa, xa + 0.35, xa + 0.35, xa], [y + 0.12, y + 0.12, y - 0.12, y - 0.12], color=color, lw=1.4)
                ax.annotate("", (xa, y - 0.12), (xa + 0.05, y - 0.12), arrowprops=dict(arrowstyle="->", color=color))
                ax.text(xa + 0.42, y, text, fontsize=8, va="center", color=color)
            else:
                ax.annotate("", (xb, y), (xa, y), arrowprops=dict(arrowstyle="->", color=color, lw=1.4, ls="--" if dashed else "-"))
                ax.text((xa + xb) / 2, y + 0.1, text, ha="center", va="bottom", fontsize=8, color=color)
        elif k == "note":
            _, x0, text, fc = it
            ax.text(xs[x0], y, text, fontsize=8, style="italic", va="center", bbox=dict(boxstyle="round", fc=fc, ec="gray"))
        elif k == "frame":
            _, a, b, label, rows = it
            top = y + 0.3; bot = top - rows * 0.52 - 0.2
            ax.add_patch(Rectangle((xs[a] - 0.45, bot), xs[b] - xs[a] + 0.9, top - bot, fill=False, ec="#555", lw=1.2))
            ax.text(xs[a] - 0.4, top - 0.05, label, fontsize=8.5, weight="bold", color="#8a1c1c" if "до оптимизации" in label else "#1c5d1c", va="top")
            y -= 0.4
            continue
        y -= 0.52
    if title: ax.set_title(title, fontsize=10)
    fig.savefig(path, dpi=170, bbox_inches="tight"); plt.close(fig)

P = ["Руководитель /\nнаблюдатель", "MeasurementService", "AccessPolicy,\n_check()", "Repository\n(приборы, сотрудники, смены)", "БД SQLite\nи AuditLog"]
RED, GRN = "#b71c1c", "#1b5e20"
draw("diagrams/seq_mass_before.png", P, [
    ("m", 0, 1, "N вызовов record(actor, kind, value, …)", False, "k"),
    ("frame", 1, 4, "loop [для каждой из N записей] — до оптимизации", 6),
    ("m", 1, 2, "require(RECORD_MEASUREMENT); _check()", False, "k"),
    ("m", 1, 3, "get(прибор), get(сотрудник), get(смена) — 3 SELECT", False, RED),
    ("m", 3, 4, "SELECT … WHERE id = ? (×3)", False, RED),
    ("m", 1, 4, "tx { INSERT measurement; INSERT audit_log }", False, "k"),
    ("m", 4, 1, "COMMIT (fsync) — на каждую запись", True, RED),
    ("m", 1, 0, "id измерения", True, "k"),
    ("note", 1, "Итого на N записей: 3N SELECT + 2N INSERT + N COMMIT; 77,7 с при N = 50 000", "#fff9e0")],
    W=13)
draw("diagrams/seq_mass_after.png", P, [
    ("m", 0, 1, "record_many(actor, rows) — один вызов, rows — генератор", False, "k"),
    ("m", 1, 2, "require(RECORD_MEASUREMENT)", False, "k"),
    ("m", 1, 3, "lru_cache(get): приборы, сотрудники, смены (по 1 SELECT на id за партию)", False, GRN),
    ("m", 3, 4, "SELECT … WHERE id = ? (только первое обращение)", False, GRN),
    ("frame", 1, 4, "loop [для каждой записи] — после оптимизации", 3.5),
    ("m", 1, 2, "_check() — те же правила, данные из кэша", False, "k"),
    ("m", 1, 4, "INSERT measurement (в общей транзакции)", False, "k"),
    ("m", 1, 4, "каждые 5 000 строк: executemany(audit_log)", False, GRN),
    ("m", 1, 4, "один COMMIT на всю партию (при ошибке — ROLLBACK)", False, GRN),
    ("m", 1, 0, "число внесённых измерений", True, "k"),
    ("note", 1, "Итого: ≈ 14 SELECT + N INSERT + N/5000 executemany + 1 COMMIT; 1,6 с при N = 50 000", "#e8f5e9")],
    W=13)
R = ["Руководитель", "ReportService", "Repository\n(измерения, расход, НС)", "Repository\n(смены, сотрудники)", "БД SQLite"]
draw("diagrams/seq_report_before.png", R, [
    ("m", 0, 1, "build(actor, период)", False, "k"),
    ("m", 1, 2, "count_by_kind / consumption / count_by_kind(НС): WHERE substr(col,1,10) BETWEEN …", False, RED),
    ("m", 2, 4, "SCAN всей таблицы (индекс не используется) — O(N)", False, RED),
    ("frame", 1, 4, "loop [каждая активная смена] — до оптимизации", 2),
    ("m", 1, 3, "employees.list(shift.id)", False, RED),
    ("m", 3, 4, "SELECT … WHERE shift_id = ?  (запрос на смену)", False, RED),
    ("m", 1, 0, "отчёт (dict)", True, "k")], W=13)
draw("diagrams/seq_report_after.png", R, [
    ("m", 0, 1, "build(actor, период)", False, "k"),
    ("m", 1, 2, "те же три запроса, условие: col >= начало AND col <= конец + ' 23:59'", False, GRN),
    ("m", 2, 4, "SEARCH по индексу (measured_at, kind_code) — покрывающий", False, GRN),
    ("m", 1, 3, "employees.list() — один запрос на все смены; Counter((shift_id, специальность))", False, GRN),
    ("m", 3, 4, "SELECT … FROM employees", False, GRN),
    ("m", 1, 0, "отчёт (dict) — идентичен прежнему", True, "k")], W=13)
