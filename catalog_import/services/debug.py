import cv2


def draw_boxes(image_path, rows, output_path="debug_boxes.png"):

    image = cv2.imread(image_path)

    for row in rows:

        pts = row["bbox"]

        x1 = int(pts[0][0])
        y1 = int(pts[0][1])

        x2 = int(pts[2][0])
        y2 = int(pts[2][1])

        cv2.rectangle(
            image,
            (x1, y1),
            (x2, y2),
            (0, 255, 0),
            2,
        )

        cv2.putText(
            image,
            row["text"],
            (x1, max(20, y1 - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(output_path, image)

    print(f"\nSaved debug image: {output_path}")