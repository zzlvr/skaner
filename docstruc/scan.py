"""Phone photo of a document → clean "scanned" page, like Adobe Scan.

1. Find the sheet: the largest four-cornered outline that covers a good part of the photo.
2. Straighten it: perspective warp to a flat rectangle (crops away the table, fingers, background).
3. Clean it: flatten uneven lighting and shadows so the paper turns white and the text stays sharp.

If no sheet outline is found with confidence (e.g. a white receipt on a white table, or several
documents in one photo) the photo is not cropped, only cleaned, so nothing is ever cut off.
"""

from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageOps

DETECT_HEIGHT = 1000       # px, detection runs on a downscaled copy
MIN_SHEET_AREA = 0.30      # the sheet must cover at least 30% of the photo to be cropped
MAX_SIDE = 2400            # px, output size cap (sharp on A4, small enough to send)
BORDER_MARGIN = 0.015      # a corner closer than 1.5% to the frame edge means "not a whole sheet"


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Top-left, top-right, bottom-right, bottom-left."""
    pts = pts.reshape(4, 2).astype("float32")
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")


def _touches_border(approx: np.ndarray, shape: tuple) -> bool:
    h, w = shape[:2]
    mx, my = BORDER_MARGIN * w, BORDER_MARGIN * h
    pts = approx.reshape(-1, 2)
    return bool(((pts[:, 0] < mx) | (pts[:, 0] > w - mx) | (pts[:, 1] < my) | (pts[:, 1] > h - my)).any())


def find_sheet(image: np.ndarray) -> np.ndarray | None:
    """Corners of the document sheet in `image` coordinates, or None if not found with confidence."""
    scale = DETECT_HEIGHT / image.shape[0]
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    area_total = small.shape[0] * small.shape[1]

    candidates = []
    edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((5, 5), np.uint8), iterations=2)
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)  # light paper on a darker table
    for mask in (edges, bright):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            hull = cv2.convexHull(contour)
            approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
            area = cv2.contourArea(approx)
            if len(approx) == 4 and cv2.isContourConvex(approx) and area >= MIN_SHEET_AREA * area_total:
                # A sheet photographed for scanning lies fully inside the frame; outlines that run into the
                # frame edge are a table, a laptop or the photo border itself, and cropping to them is wrong.
                if not _touches_border(approx, small.shape):
                    candidates.append((area, approx))
    if not candidates:
        return None
    _, best = max(candidates, key=lambda c: c[0])
    if cuts_content(gray, best):
        return None
    return _order_corners(best) / scale


CUT_INK_RATIO = 0.05  # text just outside the outline, relative to the text inside it


def cuts_content(gray: np.ndarray, outline: np.ndarray) -> bool:
    """True when there is text right outside `outline`: then the outline is not the sheet's edge but, for
    example, the edge of a shadow across the sheet, and cropping to it would cut off part of the document.
    Ink is measured by contrast to the local paper brightness, so text inside a shadow still counts.
    (QA 2026-10-25: a shadow over the right third of an act made the crop drop its prices and sums.)"""
    h, w = gray.shape[:2]
    k = max(9, int(min(h, w) * 0.015) | 1)
    closed = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    closed = closed.astype(np.float32)
    ink = (closed - gray.astype(np.float32)) > np.maximum(0.25 * closed, 12)
    inside = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(inside, outline.reshape(-1, 2).astype(np.int32), 1)
    near = cv2.dilate(inside, np.ones((int(0.01 * h) | 1,) * 2, np.uint8))
    far = cv2.dilate(inside, np.ones((int(0.07 * h) | 1,) * 2, np.uint8))
    band = (far == 1) & (near == 0)
    if not band.any() or not inside.any():
        return False
    ink_inside = ink[inside == 1].mean()
    return ink_inside > 0 and ink[band].mean() > CUT_INK_RATIO * ink_inside


def straighten(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = corners
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(corners, target)
    return cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_CUBIC)


def clean(image: np.ndarray) -> np.ndarray:
    """Remove shadows and uneven light: divide each channel by its blurred background."""
    kernel = max(31, (min(image.shape[:2]) // 25) | 1)
    channels = []
    for channel in cv2.split(image):
        background = cv2.medianBlur(cv2.dilate(channel, np.ones((7, 7), np.uint8)), kernel)
        flat = cv2.divide(channel, background, scale=255)
        channels.append(flat)
    result = cv2.merge(channels)
    # Gentle contrast: keep greys of stamps and photos, push paper to white and ink to dark.
    return cv2.convertScaleAbs(result, alpha=1.15, beta=-25)


Image.MAX_IMAGE_PIXELS = 50_000_000  # refuse decompression bombs instead of running out of memory


def scan_image(data: bytes) -> tuple[bytes, int, int, bool]:
    """Returns JPEG bytes of the cleaned page, its size, and whether the sheet was found and cropped."""
    pil = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    image = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    corners = find_sheet(image)
    if corners is not None:
        image = straighten(image, corners)
    image = clean(image)
    height, width = image.shape[:2]
    factor = min(1.0, MAX_SIDE / max(height, width))
    if factor < 1.0:
        image = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise ValueError("could not encode the scanned page")
    return jpeg.tobytes(), image.shape[1], image.shape[0], corners is not None
