from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OCRBox:
    text: str
    confidence: float
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y_center(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    boxes: list[OCRBox]
    source: str
    warnings: list[str]


def _scale_box(box: OCRBox, width: float, height: float) -> OCRBox:
    return OCRBox(
        text=box.text,
        confidence=box.confidence,
        x0=box.x0 / width * 1000,
        top=box.top / height * 1000,
        x1=box.x1 / width * 1000,
        bottom=box.bottom / height * 1000,
    )


def _split_ocr_text_box(
    text: str, 
    confidence: float, bbox: list[list[float]]
) -> list[OCRBox]:
    x_values = [float(point[0]) for point in bbox]
    y_values = [float(point[1]) for point in bbox]
    x0, x1 = min(x_values), max(x_values)
    top, bottom = min(y_values), max(y_values)
    matches = list(re.finditer(r"\S+", text))
    if not matches:
        return []
    text_width = max(1, len(text))
    output = []
    for match in matches:
        token_left = x0 + (x1 - x0) * match.start() / text_width
        token_right = x0 + (x1 - x0) * match.end() / text_width
        output.append(
            OCRBox(match.group(), confidence, token_left, top, token_right, bottom)
        )
    return output


def _deskew_and_denoise(image: Any) -> Any:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV and NumPy are required to preprocess scanned PDF pages."
        ) from exc

    array = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    points = cv2.findNonZero(binary)
    if points is not None:
        angle = cv2.minAreaRect(points)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if 0.15 < abs(angle) < 15:
            height, width = gray.shape
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
            gray = cv2.warpAffine(
                gray,
                matrix,
                (width, height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
    denoised = cv2.fastNlMeansDenoising(
        gray, None, h=10, templateWindowSize=7, searchWindowSize=21
    )
    return cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR)


class PaddlePageOCR:
    def __init__(self, use_gpu: bool = False) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR is required for scanned-page extraction. "
                "Install entry_point/requirements.txt."
            ) from exc
        self._engine = PaddleOCR(
            use_angle_cls=True, lang="en", show_log=False, use_gpu=use_gpu
        )

    def recognize(self, image: Any) -> list[OCRBox]:
        processed = _deskew_and_denoise(image)
        result = self._engine.ocr(processed, cls=True)
        boxes: list[OCRBox] = []
        for page_result in result or []:
            for entry in page_result or []:
                try:
                    polygon, (text, confidence) = entry
                    if text and str(text).strip():
                        boxes.extend(
                            _split_ocr_text_box(
                                str(text), float(confidence), polygon
                            )
                        )
                except (TypeError, ValueError, IndexError):
                    logger.debug("Ignoring malformed OCR result box.")
        return boxes


def extract_pdf_pages(
    filepath: str | Path, *, force_ocr: bool = False, use_gpu: bool = False
) -> list[ExtractedPage]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber is required to read PDF statements. "
            "Install entry_point/ocr/requirements.txt."
        ) from exc

    ocr: PaddlePageOCR | None = None
    extracted: list[ExtractedPage] = []
    with pdfplumber.open(str(filepath)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            use_page_ocr = force_ocr or len(page_text.strip()) < 50
            warnings: list[str] = []
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
                ExtractedPage(page_number, text, boxes, source, warnings)
            )
    return extracted
