import os
import cv2


def extract_cards(image_path):

    image = cv2.imread(image_path)

    if image is None:
        raise Exception(f"Unable to read image: {image_path}")

    height, width = image.shape[:2]

    # ==========================
    # Adjust according to catalog
    # ==========================

    top_margin = int(height * 0.14)
    bottom_margin = int(height * 0.03)

    left_margin = int(width * 0.03)
    right_margin = int(width * 0.03)

    working = image[
        top_margin:height - bottom_margin,
        left_margin:width - right_margin,
    ]

    h, w = working.shape[:2]

    rows = 3
    cols = 4

    cell_h = h // rows
    cell_w = w // cols

    output_dir = "cards"

    os.makedirs(output_dir, exist_ok=True)

    # Delete previous crops
    for file in os.listdir(output_dir):
        os.remove(os.path.join(output_dir, file))

    index = 1

    print()
    print("=" * 80)
    print("GRID CROPPING")
    print("=" * 80)

    for r in range(rows):

        for c in range(cols):

            x1 = c * cell_w
            y1 = r * cell_h

            x2 = (c + 1) * cell_w
            y2 = (r + 1) * cell_h

            crop = working[y1:y2, x1:x2]

            filename = os.path.join(
                output_dir,
                f"card_{index}.png"
            )

            cv2.imwrite(filename, crop)

            print(filename)

            index += 1

    print()
    print(f"Total Cards : {index-1}")