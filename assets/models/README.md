# Model 3D (GLB)

Taruh file model `.glb` di direktori ini. File `.glb` **tidak** ikut ke git
(lihat `.gitignore`) karena ukurannya besar dan bersifat personal.

Dashboard memuat model berikut bila ada (nama file harus persis):

| File                  | Untuk                              |
|-----------------------|------------------------------------|
| `<nama-akun-1>.glb`   | karakter 3D akun pertama           |
| `<nama-akun-2>.glb`   | karakter 3D akun kedua             |
| `sofa_couch.glb`      | sofa di scene                      |
| `bmo_robot.glb`       | robot hiasan                       |
| `cat.glb`             | hewan hiasan                       |
| `monitoring_station.glb` | monitor di meja                 |

Bila file tidak ada, dashboard tetap jalan — karakter/objek memakai bentuk
prosedural bawaan. Endpoint `/assets/*.glb` hanya melayani file `.glb`
(di belakang auth, anti path-traversal).
