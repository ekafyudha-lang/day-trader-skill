---
name: trader-idx
description: >-
  Saham Indonesia (IDX / Bursa Efek Indonesia). Applies the IDX rules the US
  skills do not know: fraksi harga (tick size), ARA and ARB price limits, 1 lot
  = 100 lembar, broker fees with the 0.1 % sales tax, and the WIB sessions
  with the lunch break. Sizes a position in lots from modal and risk, and
  backtests four daily strategies (breakout, pullback, rsi2, volspike) plus an
  intraday opening range breakout on free Yahoo .JK data or your own CSV,
  reporting win rate, average net R after fees, profit factor and an honest
  verdict. Use when the user names an Indonesian ticker (DOOH, BBCA, TLKM,
  GOTO, BBRI...), says "saham", "IDX", "BEI", "lot", "ARA", "ARB", "fraksi",
  "metode apa yang win rate-nya tinggi", "backtest saham Indonesia", "berapa
  lot", or asks which method works for a .JK stock.
argument-hint: "[KODE SAHAM] [rules|size|backtest|orb|scan]"
---

# trader-idx

Skill ini untuk saham di Bursa Efek Indonesia. Skill lain di pack ini dibuat
untuk pasar AS (jam 09:30 sampai 16:00 New York, harga per lembar, tanpa
batas harian). BEI berbeda: harga bergerak per fraksi, ada batas ARA / ARB,
beli per lot, fee jual lebih besar karena pajak, dan ada istirahat siang.
Semua angka di sini sudah memperhitungkan itu.

Trading harian membuat kebanyakan orang rugi. Backtest yang bagus tidak
mengubah hal itu.

`${CLAUDE_SKILL_DIR}` di bawah adalah folder skill ini (tempat SKILL.md ini).

```bash
python3 "${CLAUDE_SKILL_DIR}/idx.py" rules 122 --prev 118                   # fraksi, ARA, ARB, nilai 1 lot
python3 "${CLAUDE_SKILL_DIR}/idx.py" size --modal 10000000 --risk-pct 1 --entry 122 --stop 114
python3 "${CLAUDE_SKILL_DIR}/idx.py" backtest DOOH                          # 4 strategi harian, 2 tahun
python3 "${CLAUDE_SKILL_DIR}/idx.py" backtest DOOH --strategy breakout --target-r 2 --trades
python3 "${CLAUDE_SKILL_DIR}/idx.py" backtest DOOH --csv DOOH.csv           # data sendiri
python3 "${CLAUDE_SKILL_DIR}/idx.py" orb DOOH                               # ORB intraday 5 menit, 60 hari
python3 "${CLAUDE_SKILL_DIR}/idx.py" scan DOOH BBCA TLKM GOTO               # semua strategi, urut expectancy
```

Flag lain: `--target-r` (default 2), `--atr-mult` (default 1.5), `--hold`
(maks hari pegang, default 10), `--min-value` (nilai transaksi rata-rata 20
hari minimum, default Rp 500 juta; `0` mematikan filter), `--fee-buy` /
`--fee-sell` (persen, default 0.15 / 0.25), `--arb` (default 15),
`--arb-tiered`, `--range` (default `2y`), `--offline FILE`, `--json`.

## Aturan BEI yang dipakai

| aturan | nilai default |
| --- | --- |
| Fraksi harga | < 200: 1, 200 sampai < 500: 2, 500 sampai < 2.000: 5, 2.000 sampai < 5.000: 10, >= 5.000: 25 |
| Lot | 1 lot = 100 lembar |
| Harga minimum | 50 (papan utama dan pengembangan) |
| ARA | 35 % (harga 50 sampai 200), 25 % (> 200 sampai 5.000), 20 % (> 5.000) dari penutupan kemarin |
| ARB | 15 % untuk semua harga (`--arb`, atau `--arb-tiered` untuk ikut tingkatan ARA) |
| Fee | beli 0,15 %, jual 0,25 % (termasuk PPh final 0,1 %) |
| Sesi (WIB) | Senin sampai Kamis 09:00 sampai 12:00 dan 13:30 sampai 15:50; Jumat 09:00 sampai 11:30 dan 14:00 sampai 15:50 |

Aturan ini bisa berubah. Cek idx.co.id dan fee broker Anda sendiri, lalu
pakai flag untuk menyesuaikan. Papan akselerasi dan saham di papan
pemantauan khusus (full call auction) punya aturan lain dan tidak dimodelkan.

## Cara backtest bekerja

1. **Sinyal** dibaca pada harga PENUTUPAN hari t. **Beli** di harga
   PEMBUKAAN hari t+1, bukan di penutupan hari sinyal, jadi tidak ada
   "melihat masa depan".
2. Sinyal **dilewati** bila hari beli terkunci ARA (open = high = low, tidak
   ada penjual), tanpa volume, atau nilai transaksi rata-rata 20 hari di bawah
   `--min-value`. Saham tipis memakan edge lewat spread dan antrean.
3. **Stop** = harga beli dikurangi 1,5 x ATR(14), dibulatkan ke bawah sesuai
   fraksi. **Target** = harga beli + 2 x risiko, dibulatkan ke atas.
4. Setiap hari stop dicek lebih dulu dari target, jadi hari yang menyentuh
   keduanya dihitung kena stop. Gap melewati stop atau target terisi di
   harga pembukaan. Hari yang terkunci ARB tidak bisa dijual: trade menunggu
   hari berikutnya. Setelah `--hold` hari, keluar di penutupan.
5. **R bersih fee**: (jual x (1 - fee jual) - beli x (1 + fee beli)) / risiko.
   Menang = R bersih > 0. Satu posisi per strategi pada satu waktu.

| strategi | sinyal |
| --- | --- |
| `breakout` | tutup di atas high tertinggi 20 hari sebelumnya, volume > 1,5 x rata-rata 20 hari |
| `pullback` | MA20 > MA50, low menyentuh MA20, tutup kembali di atasnya, candle hijau |
| `rsi2` | RSI(2) < 10 saat harga di atas MA50 (koreksi dalam tren naik) |
| `volspike` | volume > 3 x rata-rata, naik dari kemarin, tutup di 25 % teratas range hari itu |
| `orb` | (intraday) close 5 menit pertama di atas range 15 menit pertama sesi 1, stop di low range (`--stop-rule mid` untuk tengah), keluar di target, stop, atau bar terakhir sebelum 15:50 |

**Verdict**: di bawah 30 trade "Sampel terlalu sedikit". Rata-rata R >= +0,10
dan profit factor >= 1,2: "Ada edge historis". Rata-rata R <= -0,10:
"Negatif". Di antaranya: "Mendekati impas".

## Langkah

1. Kalau pengguna bertanya "metode apa yang win rate-nya tinggi untuk X",
   jalankan `backtest X` (semua strategi), lalu `orb X` kalau mereka day
   trading. Untuk beberapa saham sekaligus pakai `scan`.
2. Sampaikan per strategi: jumlah trade, win rate, rata-rata R bersih, PF,
   rugi beruntun maksimum, dan baris verdict persis seperti dicetak.
3. Jelaskan bahwa **win rate saja tidak cukup**. Target 2R cukup menang
   sekitar 33 % untuk impas; target 0,5R butuh sekitar 67 %. Yang menentukan
   adalah rata-rata R setelah fee.
4. Kalau semua strategi "Sampel terlalu sedikit" atau "Negatif", katakan
   terus terang. Jangan menyulap angka kecil jadi edge.
5. Kalau banyak sinyal dilewati karena likuiditas, sampaikan: saham itu
   tipis, dan hasil nyata akan lebih buruk dari backtest.
6. Kalau pengguna ingin masuk, hitung lot dengan `size`, lalu cek `rules`
   untuk harga ARA / ARB hari itu. Sebutkan rugi bila kena stop dalam rupiah.

## Aturan

- Setiap angka berasal dari hasil tool. Jangan membulatkan "impas" jadi
  "menguntungkan".
- Backtest adalah data masa lalu, bukan prediksi. Katakan sekali, jelas.
- Data Yahoo gratis dan tidak resmi, bisa terlambat atau bolong. Bar harian
  tidak tahu urutan high dan low di dalam hari itu, jadi stop dihitung lebih
  dulu (anggapan konservatif).
- Stop di BEI bukan jaminan: saham yang terkunci ARB bisa tidak terjual
  berhari-hari.

## Yang tidak pernah dilakukan

Tidak pernah memasang order, tidak pernah menyebut hasil sebagai kepastian,
tidak pernah memakai bar yang belum selesai, dan tidak pernah memakai
penutupan hari sinyal sebagai harga beli.

## Safety

- Education and tooling, not financial advice. Bukan saran investasi.
- Paper trading dulu. Masuk dengan uang sungguhan adalah keputusan pengguna
  sendiri.
- Skill ini tidak bisa mengirim order. Tidak ada order tanpa yes eksplisit
  dari pengguna untuk order itu, lewat `/trader-broker` dan `/trader-risk`.
- Tanpa margin, short selling atau leverage.
