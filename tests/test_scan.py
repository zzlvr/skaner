import cv2
import numpy as np

from docstruc.scan import cuts_content, scan_image


def _photo(corners, size=(1600, 1200), paper=245, table=70):
    """A white sheet with some 'text' lines placed at `corners` on a dark table."""
    h, w = size
    sheet = np.full((1100, 800, 3), paper, np.uint8)
    for y in range(120, 1000, 60):
        cv2.line(sheet, (80, y), (720, y), (30, 30, 30), 6)
    photo = np.full((h, w, 3), table, np.uint8)
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [800, 0], [800, 1100], [0, 1100]]), np.float32(corners))
    warped = cv2.warpPerspective(sheet, m, (w, h))
    mask = cv2.warpPerspective(np.full((1100, 800), 255, np.uint8), m, (w, h))
    photo[mask > 0] = warped[mask > 0]
    ok, jpeg = cv2.imencode(".jpg", photo)
    return jpeg.tobytes()


def test_sheet_in_perspective_is_cropped_and_straightened():
    data = _photo([[260, 180], [930, 230], [980, 1420], [200, 1380]])
    _, width, height, cropped = scan_image(data)
    assert cropped
    assert 1.3 < height / width < 1.6  # roughly the 800×1100 sheet proportions


def test_outline_touching_the_frame_is_not_cropped():
    data = _photo([[0, 0], [1199, 0], [1199, 1599], [0, 1599]])
    *_, cropped = scan_image(data)
    assert not cropped


def test_no_sheet_found_keeps_the_whole_photo():
    blank = np.full((900, 700, 3), 230, np.uint8)
    ok, jpeg = cv2.imencode(".jpg", blank)
    _, width, height, cropped = scan_image(jpeg.tobytes())
    assert not cropped and (width, height) == (700, 900)



def test_outline_through_the_text_is_not_a_sheet():
    """A soft shadow over the right third of a photographed act made the outline of the lit part
    look like the sheet, and the PDF-scan dropped the prices and sums. Text right outside an outline = no crop."""
    gray = np.full((1000, 800), 70, np.uint8)
    gray[100:900, 100:700] = 240  # the sheet
    for y in range(160, 860, 40):
        cv2.line(gray, (140, y), (660, y), 30, 4)  # text lines across the whole sheet
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    sheet = np.array([[100, 100], [699, 100], [699, 899], [100, 899]], np.float32)
    lit_part = np.array([[100, 100], [480, 100], [480, 899], [100, 899]], np.float32)
    assert not cuts_content(gray, sheet)
    assert cuts_content(gray, lit_part)
    gray[:, 480:] = (gray[:, 480:] * 0.45).astype(np.uint8)  # the text inside the shadow still counts
    assert cuts_content(gray, lit_part)
