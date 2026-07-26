import cv2
import numpy as np


class LayoutDetector:

    def __init__(self, image_path):

        self.image = cv2.imread(image_path)

        if self.image is None:
            raise Exception("Image not found.")

        self.gray = cv2.cvtColor(
            self.image,
            cv2.COLOR_BGR2GRAY,
        )

    def preprocess(self):

        blur = cv2.GaussianBlur(
            self.gray,
            (5, 5),
            0,
        )

        thresh = cv2.threshold(
            blur,
            240,
            255,
            cv2.THRESH_BINARY_INV,
        )[1]

        return thresh

    def detect_lines(self):

        thresh = self.preprocess()

        horizontal_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (120, 1),
        )

        vertical_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (1, 120),
        )

        horizontal = cv2.morphologyEx(
            thresh,
            cv2.MORPH_OPEN,
            horizontal_kernel,
        )

        vertical = cv2.morphologyEx(
            thresh,
            cv2.MORPH_OPEN,
            vertical_kernel,
        )

        return horizontal, vertical

    def debug(self):

        horizontal, vertical = self.detect_lines()

        cv2.imwrite(
            "debug_horizontal.png",
            horizontal,
        )

        cv2.imwrite(
            "debug_vertical.png",
            vertical,
        )

        print("\nSaved:")
        print("debug_horizontal.png")
        print("debug_vertical.png")