"""PDF 正文解析（macOS 原生 Quartz/PDFKit + Vision OCR）

从已下载的简历 PDF 文件读全文，三级递进：
1. extract_pdf_text_quartz — 读 PDF 文本层（有文本层的 PDF，最快最准）
2. ocr_pdf_text           — 文本层为空（扫描件/图片型 PDF）时，逐页渲染 + Vision OCR
3. extract_resume_from_pdf — 组合入口：先文本层，空了再 OCR 兜底

用于：
- collect 收集时把附件简历正文入库（比 BOSS 预览的 AX 树更可靠）
- 评分前对「有附件文件但正文为空」的候选人做二次回捞（消除评分盲区）

纯解析，无 cua-driver / 网络依赖；pyobjc / Vision 不可用时优雅降级返回 ""。
"""
import re

# OCR 默认上限：简历一般 1-3 页，限页防扫描件异常大；渲染倍率提升小字识别率
_OCR_MAX_PAGES = 12
_OCR_SCALE = 2.0
_OCR_LANGS = ("zh-Hans", "en-US")

_WATERMARK_PATTERNS = (
    re.compile(r'^[A-Za-z0-9_/+\-]{20,}~+$'),  # base64 水印(以波浪号收尾)
    re.compile(r'^[a-f0-9]{20,}~*$'),           # 纯 hex 水印(兼容旧形态)
    re.compile(r'^[~\-]{1,3}$'),                 # 水印残留的孤立波浪号/横线行
)


def _clean(raw: str) -> str:
    """去 BOSS 水印 token + 空行规整。"""
    out = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if any(p.match(s) for p in _WATERMARK_PATTERNS):
            continue
        out.append(s)
    return "\n".join(out).strip()


def _name_ok(text: str, expected_name: str) -> bool:
    """归属校验：姓名整体出现，或被排版拆字时逐字均在全文。空名/空文本视为通过。"""
    if not (expected_name and text and len(expected_name) >= 2):
        return True
    return expected_name in text or all(c in text for c in expected_name)


def extract_pdf_text_quartz(pdf_path: str, expected_name: str = "") -> str:
    """用 macOS 原生 Quartz/PDFKit 直接解析 PDF 文本层。

    比 BOSS 预览的 AX 树提取更可靠：读全部页、不受滚动/渲染时序/AX 截断影响。
    pyobjc 不可用、解析失败、或姓名不匹配(串档)时返回 ""。
    """
    if not pdf_path:
        return ""
    try:
        import Quartz
        from Foundation import NSURL
    except Exception:
        return ""  # pyobjc/Quartz 不可用 → 优雅降级
    try:
        url = NSURL.fileURLWithPath_(str(pdf_path))
        doc = Quartz.PDFDocument.alloc().initWithURL_(url)
        if doc is None:
            return ""
        raw = doc.string() or ""
    except Exception:
        return ""

    text = _clean(raw)
    return text if _name_ok(text, expected_name) else ""


def ocr_pdf_text(
    pdf_path: str,
    expected_name: str = "",
    max_pages: int = _OCR_MAX_PAGES,
) -> str:
    """对图片型/扫描件 PDF 逐页渲染成位图 + macOS Vision OCR 识别中文+英文。

    仅当 PDF 没有文本层（extract_pdf_text_quartz 返回空）时才需要调用。
    pyobjc-framework-Vision 未安装 / 渲染或识别失败时返回 ""。
    """
    if not pdf_path:
        return ""
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except Exception:
        # 缺 Vision 绑定 → 提示一次安装方式后降级
        print("    ⚠ 未安装 Vision OCR 绑定，跳过 OCR"
              "（如需: pip install pyobjc-framework-Vision）")
        return ""

    try:
        url = NSURL.fileURLWithPath_(str(pdf_path))
        pdf = Quartz.CGPDFDocumentCreateWithURL(url)
        if not pdf:
            return ""
        n = Quartz.CGPDFDocumentGetNumberOfPages(pdf)
    except Exception:
        return ""

    pages_text = []
    for i in range(1, min(n, max_pages) + 1):
        try:
            txt = _ocr_one_page(Quartz, Vision, pdf, i)
        except Exception:
            txt = ""
        if txt:
            pages_text.append(txt)

    text = _clean("\n".join(pages_text))
    return text if _name_ok(text, expected_name) else ""


def _ocr_one_page(Quartz, Vision, pdf, index: int) -> str:
    """渲染单页 → CGImage → Vision 文本识别 → 拼接识别行。"""
    page = Quartz.CGPDFDocumentGetPage(pdf, index)
    if not page:
        return ""
    rect = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
    w = max(1, int(rect.size.width * _OCR_SCALE))
    h = max(1, int(rect.size.height * _OCR_SCALE))

    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, 0, cs, Quartz.kCGImageAlphaNoneSkipLast)
    if ctx is None:
        return ""
    # 白底（扫描件透明区域填白，利于识别）
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    Quartz.CGContextFillRect(ctx, ((0, 0), (w, h)))
    Quartz.CGContextScaleCTM(ctx, _OCR_SCALE, _OCR_SCALE)
    Quartz.CGContextTranslateCTM(ctx, -rect.origin.x, -rect.origin.y)
    Quartz.CGContextDrawPDFPage(ctx, page)
    img = Quartz.CGBitmapContextCreateImage(ctx)
    if img is None:
        return ""

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    # 准确模式(默认) + 中英文 + 语言纠正
    req.setRecognitionLanguages_(list(_OCR_LANGS))
    req.setUsesLanguageCorrection_(True)
    handler.performRequests_error_([req], None)

    lines = []
    for obs in (req.results() or []):
        cand = obs.topCandidates_(1)
        if cand and len(cand):
            lines.append(cand[0].string())
    return "\n".join(lines)


def extract_resume_from_pdf(
    pdf_path: str,
    expected_name: str = "",
    ocr_fallback: bool = True,
) -> tuple[str, str]:
    """简历正文统一入口：先读文本层，空了再 OCR 兜底。

    返回 (正文, 来源)；来源 ∈ {"text", "ocr", ""}。空字符串表示均失败。
    """
    text = extract_pdf_text_quartz(pdf_path, expected_name)
    if text:
        return text, "text"
    if ocr_fallback:
        text = ocr_pdf_text(pdf_path, expected_name)
        if text:
            return text, "ocr"
    return "", ""


def extract_contacts(text: str) -> tuple[str, str]:
    """从简历正文提取 (手机号, 邮箱)。无标签依赖，直接搜模式。

    手机号: 11 位 1 开头。
    邮箱: 标准邮箱模式；剥离前面粘连的中文/英文标签（如「邮箱：」「Email:」），
          BOSS 简历常出现「邮箱：x@y.com」无空格粘连，旧正则会把标签也吞进去。
    """
    phone = email = ""
    if not text:
        return phone, email
    pm = re.search(r'(?<!\d)(1[3-9]\d{9})(?!\d)', text)
    if pm:
        phone = pm.group(1)
    em = re.search(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
    if em:
        email = em.group(1).rstrip('.,;:）)')
    return phone, email
