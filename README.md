# Video Redactor

使用 Python、PySide6 與 OpenCV 製作的桌面影片遮罩與裁剪工具。

## 功能

- 框選、移動及縮放黑色矩形遮罩，設定各遮罩的作用時間。
- 透過手動關鍵影格設定遮罩移動，支援線性插值與位置跳轉。
- 保留指定起訖範圍，並刪除多個中間片段。
- 逐幀預覽、縮放畫面，匯出無聲 MP4。

## 執行檔版本

提供 Windows x64 免安裝版，無須另外安裝 Python 或套件。

1. 下載 [VideoRedactor-v0.1.0-windows-x64.zip](https://github.com/Harvey-L6/video-redactor_demo/releases/download/v0.1.0/VideoRedactor-v0.1.0-windows-x64.zip)。
2. 將 ZIP 完整解壓縮。
3. 雙擊資料夾內的 `VideoRedactor.exe` 即可啟動。

請保留 `_internal` 資料夾，不要單獨搬走執行檔。[`.sha256`](https://github.com/Harvey-L6/video-redactor_demo/releases/download/v0.1.0/VideoRedactor-v0.1.0-windows-x64.zip.sha256) 為選用的下載完整性校驗檔，不影響程式執行。

## 從原始碼執行

已於 Windows、Python 3.13 環境驗證。先安裝 Python 3.13，再於專案根目錄使用 PowerShell 執行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\src\gui.py
```

套件安裝於專案內的 `.venv` 虛擬環境。

## 使用方式

1. 點選「選擇影片」，開啟 MP4 檔案。
2. 在「隱私遮罩」框選遮罩，調整位置、大小與作用時間；需要移動遮罩時，新增關鍵影格位置點。
3. 在「影片裁剪」設定保留範圍與刪除區段。
4. 點選「匯出無聲 MP4」，選擇儲存位置。

## 注意事項

- 遮罩與關鍵影格皆需手動設定，沒有自動偵測或追蹤功能。
- 編輯預覽使用半透明遮罩，匯出時才套用不透明黑框；裁剪區段也在匯出時生效。
- 輸出不保留原始音訊。分享前請完整播放成品，確認遮罩位置與作用時間。
