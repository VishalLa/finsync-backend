from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import List, Optional

from ._ocr import PaddlePageOCR
from .common import ExtractedPage, OCRBox

def _scale_box(box: OCRBox, width: float, height: float) -> OCRBox:
    return OCRBox(
        text=box.text,
        confidence=box.confidence,
        x0=box.x0 / width * 1000,
        top=box.top / height * 1000,
        x1=box.x1 / width * 1000,
        bottom=box.bottom / height * 1000,
    )


def extract_pdf_pages(
    filepath: str | Path, *, force_ocr: bool = False, use_gpu: bool = False
) -> List[ExtractedPage]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber is required to read PDF statements. "
            "Install entry_point/ocr/requirements.txt."
        ) from exc

    ocr: Optional[PaddlePageOCR] = None
    extracted: List[ExtractedPage] = []
    
    with pdfplumber.open(str(filepath)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            use_page_ocr = force_ocr or len(page_text.strip()) < 50
            warnings: List[str] = []
            
            if use_page_ocr:
                if ocr is None:
                    ocr = PaddlePageOCR(use_gpu=use_gpu)
                    
                image = page.to_image(resolution=250).original
                image_width, image_height = image.size
                boxes = [
                    _scale_box(box, image_width, image_height)
                    for box in ocr.recognize(image)
                ]
                text = " ".join(box.text for box in boxes)
                source = "paddleocr"
                if boxes and median(box.confidence for box in boxes) < 0.8:
                    warnings.append(
                        f"Page {page_number}: median OCR confidence is below 0.80."
                    )
                
            else:
                boxes = [
                    _scale_box(
                        OCRBox(
                            text=str(word["text"]),
                            confidence=1.0,
                            x0=float(word["x0"]),
                            top=float(word["top"]),
                            x1=float(word["x1"]),
                            bottom=float(word["bottom"]),
                        ),
                        float(page.width),
                        float(page.height),
                    )
                    for word in page.extract_words()
                    if str(word.get("text", "")).strip()
                ]
                text = page_text
                source = "pdfplumber"
            
            extracted.append(
                ExtractedPage(
                    page_number=page_number, 
                    text=text, 
                    boxes=boxes, 
                    source=source, 
                    warnings=warnings
                )
            )
    
    return extracted
