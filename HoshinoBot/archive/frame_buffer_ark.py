# -*- coding:  utf-8 -*-
import ctypes
import sys
import time
import win32gui, win32con, win32api
user32 = ctypes.windll.user32


class FrameBuffer(object): 
    def __init__(self,
            class_name='UnityWndClass',
            title_name='Ark ReCode',
            # Using left upper corner as default
            coord=(0.04, 0.13),
            delay=20):
        # Init coordinates
        self.coord = coord
        # Default delay
        self.delay = delay
        # Find handle of the window
        self.hwnd = win32gui.FindWindow(class_name, title_name)

    def click(self):
        hwnd = self.hwnd
        lp = win32api.MAKELONG(0, 0)
        # Calculat the size of the window
        _, _, width, height = win32gui.GetClientRect(hwnd)
        x, y = int(self.coord[0] * width), int(self.coord[1] * height)
        sx, sy = win32gui.ClientToScreen(hwnd, (x, y))
        # Calculate absolute coordinates
        vx, vy, vw, vh=[user32.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        ax, ay = int((sx - vx) * 65535 / vw), int((sy - vy) * 65535 / vh)
        # Move mouse to (ax, ay) globally
        win32api.mouse_event(
            win32con.MOUSEEVENTF_ABSOLUTE | win32con.MOUSEEVENTF_MOVE, ax, ay)
        # Activate the window
        win32gui.SendMessage(hwnd, win32con.WM_ACTIVATE,
                             win32con.WA_CLICKACTIVE, 0)
        # 1. Pause to unpause
        win32api.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, 0, lp)
        win32api.Sleep(100)
        win32api.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
        # 2. Add 20ms to skip 1-2 frames
        win32api.Sleep(self.delay)
        # 3. Unpause to pause
        win32api.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, 0, lp)
        win32api.Sleep(10)
        win32api.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)

    def run(self, is_pcr=True): 
        delay = input('延迟(默认20ms)：')
        self.delay = int(delay) if delay.isdigit() else self.delay
        # Run the main function
        while True: 
            input()
            self.click()


if __name__ == '__main__': 
    fb = FrameBuffer()
    fb.run()
