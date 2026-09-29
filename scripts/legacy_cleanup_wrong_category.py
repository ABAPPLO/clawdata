import os
import sqlite3

db = r"D:\project\clawdata\data\clawdata.db"
c = sqlite3.connect(db)
rows = c.execute("SELECT id, file FROM downloads WHERE tag_category = ?", ("单人武术展示",)).fetchall()
removed_files = 0
for rid, path in rows:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            removed_files += 1
        except OSError:
            pass
c.execute("DELETE FROM downloads WHERE tag_category = ?", ("单人武术展示",))
c.commit()
print(f"删除记录 {len(rows)} 条，删除文件 {removed_files} 个")
c.close()
