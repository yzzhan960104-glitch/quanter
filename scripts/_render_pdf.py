# -*- coding: utf-8 -*-
"""渲染扫描版 PDF 指定页为 PNG（供 Read 工具视觉识别）。用法: python _render_pdf.py START END"""
import sys
import os
import fitz

path = r"C:\Users\yzzhan\Desktop\quanter\多空轉折一手抓.pdf"
doc = fitz.open(path)
start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
end = int(sys.argv[2]) if len(sys.argv) > 2 else 8
os.makedirs(r"C:\Users\yzzhan\Desktop\quanter\scripts\pages", exist_ok=True)
for i in range(start - 1, min(end, doc.page_count)):
    pix = doc[i].get_pixmap(dpi=200)
    pix.save(rf"C:\Users\yzzhan\Desktop\quanter\scripts\pages\p{i+1:03d}.png")
print(f"rendered {start}-{min(end, doc.page_count)}")
