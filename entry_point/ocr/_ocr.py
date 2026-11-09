import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from .common import OCRBox

logger = logging.getLogger(__name__)


class Block:
    """
    One detected word returned by PaddleOCR.

    Attributes
    ----------
    x, y : top-left pixel coordinates of the bounding box
    text : recognised text
    conf : recognition confidence in [0, 1]
    """

    def __init__(self, x: int, y: int, text: str, conf: float) -> None:
        self.x    = x
        self.y    = y
        self.text = text
        self.conf = conf

    def __repr__(self) -> str:
        return f"Block(y={self.y}, x={self.x}, text={self.text!r})"
    

class OCR: 
    """
    initlize paddleOCR and detect char
    """
    def __init__(self):
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR is required for scanned-page extraction. "
                "Install entry_point/requirements.txt."
            ) from exc

        self.ocr = PaddleOCR(
            use_angle_cls=True, 
            lang="en", 
            show_log=False, 
            use_gpu=False
        )


    @staticmethod
    def ocr_to_blocks(ocr_result, conf_threshold=0.25) -> List[Block]:
        """
        Convert raw PaddleOCR result to sorted Block list.
        ocr_result: output form paddleocr
        """
        blocks = []
        if not ocr_result or not ocr_result[0]:
            return blocks
        
        for line in ocr_result[0]:
            try:
                box, (text, conf) = line 

                if conf >= conf_threshold and text.strip():
                    x = int(box[0][0])
                    y = int(box[0][1])
                    blocks.append(Block(x, y, text.strip(), conf))

            except (TypeError, ValueError, IndexError):
                continue
        blocks.sort(key=lambda b:(b.y, b.x))
        return blocks
    

    def ocr_image(self, image_path: Path) -> List[Block]:
        result = self.ocr.ocr(str(image_path), cls=True)
        return self.ocr_to_blocks(result)


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
            use_angle_cls=True,
            lang="en", 
            show_log=False, 
            use_gpu=use_gpu
        )
        
    @staticmethod
    def _split_ocr_text_box(
        text: str, 
        confidence: float, 
        bbox: List[List[float]]
    ) -> List[OCRBox]:
        x_values = [float(point[0]) for point in bbox]
        y_values = [float(point[1]) for point in bbox]
        x0, x1 = min(x_values), max(x_values)
        top, bottom = min(y_values), max(y_values)
        matches = list(re.finditer(r"\S+", text))
        if not matches:
            return []

        text_width = max(1, len(text))
        output: List[OCRBox] = []
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


    def recognize(self, image: Any) -> List[OCRBox]:
        processed = PaddlePageOCR._deskew_and_denoise(image)
        result = self._engine.ocr(processed, cls=True)
        boxes: List[OCRBox] = []
        
        for page_result in result or []:
            for entry in page_result or []:
                try:
                    polygon, (text, confidence) = entry
                    if text and str(text).strip():
                        boxes.extend(
                            PaddlePageOCR._split_ocr_text_box(
                                str(text), float(confidence), polygon
                            )
                        )
                except (TypeError, ValueError, IndexError):
                    logger.debug("Ignoring malformed OCR result box.")
        return boxes



# get singleton object of OCR
ocr_instance: Optional[OCR] = None 

def get_ocr() -> OCR: 
    global ocr_instance 
    if ocr_instance is None:
        ocr_instance = OCR()
    return ocr_instance
