#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include "hdf5.h"

#define QLEN 32
/* =====================================================
 * median.c
 * Read .h5 file -> apply 3x3 Median Filter to the 
 * specified data moments -> overwrite to the .h5
 *
 * Ver 1.0
 * 26.Dec.2025
 * =====================================================
 */ 

/* ---------------------------
   HDF5 diag suppression
   --------------------------- */
static void suppress_hdf5_diag(void)
{
    H5Eset_auto(H5E_DEFAULT, NULL, NULL);
}

/* ---------------------------
   argv[2..] に quantity があるか
   --------------------------- */
static int is_target_quantity(const char *q, int argc, char **argv)
{
    for (int i = 2; i < argc; i++) {
        if (strcmp(q, argv[i]) == 0) return 1;
    }
    return 0;
}

/* ---------------------------
   文字列属性（可変長/固定長）を読む
   --------------------------- */
static int read_attr_string(hid_t obj, const char *attr_name,
                            char *buf, size_t buflen)
{
    if (buflen == 0) return 0;
    memset(buf, 0, buflen);

    if (H5Aexists(obj, attr_name) <= 0) return 0;

    hid_t a = H5Aopen(obj, attr_name, H5P_DEFAULT);
    if (a < 0) return 0;

    hid_t t = H5Aget_type(a);
    if (t < 0) { H5Aclose(a); return 0; }

    int ok = 0;

    if (H5Tis_variable_str(t)) {
        char *tmp = NULL;
        if (H5Aread(a, t, &tmp) >= 0 && tmp) {
            strncpy(buf, tmp, buflen - 1);
            buf[buflen - 1] = '\0';
            free(tmp);
            ok = (buf[0] != '\0');
        }
    } else {
        /* fixed-length string: may not be NUL-terminated */
        size_t n = H5Tget_size(t);
        if (n > 0) {
            char *tmp = (char*)malloc(n + 1);
            if (tmp) {
                memset(tmp, 0, n + 1);
                if (H5Aread(a, t, tmp) >= 0) {
                    /* fixed-length is often space-padded: trim trailing spaces */
                    size_t m = n;
                    while (m > 0 && (tmp[m-1] == ' ' || tmp[m-1] == '\0')) m--;
                    tmp[m] = '\0';

                    strncpy(buf, tmp, buflen - 1);
                    buf[buflen - 1] = '\0';
                    ok = (buf[0] != '\0');
                }
                free(tmp);
            }
        }
    }

    H5Tclose(t);
    H5Aclose(a);
    return ok;
}

/* ---------------------------
   数値属性を double で読む（型が int/float でもOK）
   --------------------------- */
static int read_attr_double(hid_t obj, const char *attr_name, double *out)
{
    *out = NAN;
    if (H5Aexists(obj, attr_name) <= 0) return 0;

    hid_t a = H5Aopen(obj, attr_name, H5P_DEFAULT);
    if (a < 0) return 0;

    hid_t t = H5Aget_type(a);
    if (t < 0) { H5Aclose(a); return 0; }

    H5T_class_t cls = H5Tget_class(t);
    int ok = 0;

    if (cls == H5T_INTEGER || cls == H5T_FLOAT) {
        if (H5Aread(a, H5T_NATIVE_DOUBLE, out) >= 0) ok = 1;
    }

    H5Tclose(t);
    H5Aclose(a);
    return ok;
}

/* ---------------------------
   /datasetN/where の nrays/nbins (ATTRIBUTE) を読む
   --------------------------- */
static void read_dims(hid_t file, const char *dataset, int *nrays, int *nbins)
{
    char path[256];
    snprintf(path, sizeof(path), "%s/where", dataset);

    hid_t g = H5Gopen(file, path, H5P_DEFAULT);
    if (g < 0) {
        fprintf(stderr, "ERROR: cannot open %s\n", path);
        exit(1);
    }

    hid_t a = H5Aopen(g, "nrays", H5P_DEFAULT);
    if (a < 0) { fprintf(stderr, "ERROR: nrays missing in %s\n", path); exit(1); }
    if (H5Aread(a, H5T_NATIVE_INT, nrays) < 0) { fprintf(stderr, "ERROR: cannot read nrays\n"); exit(1); }
    H5Aclose(a);

    a = H5Aopen(g, "nbins", H5P_DEFAULT);
    if (a < 0) { fprintf(stderr, "ERROR: nbins missing in %s\n", path); exit(1); }
    if (H5Aread(a, H5T_NATIVE_INT, nbins) < 0) { fprintf(stderr, "ERROR: cannot read nbins\n"); exit(1); }
    H5Aclose(a);

    H5Gclose(g);
}

/* ---------------------------
   raw/physicalの取り扱い補助：
   与えられた値 v が raw か physical か曖昧なとき、
   rawとして妥当なら raw、そうでなければ (v-offset)/gain を raw とみなす
   --------------------------- */
static double to_raw_value(double v, double gain, double offset,
                           double raw_min, double raw_max)
{
    if (!isfinite(v)) return NAN;

    /* raw として解釈できるか（整数かつ範囲内） */
    {
        double rv = nearbyint(v);
        if (fabs(v - rv) < 1e-6 && rv >= raw_min && rv <= raw_max) {
            return rv;
        }
    }

    /* 物理量として逆変換 */
    if (gain != 0.0) {
        double r2 = nearbyint((v - offset) / gain);
        if (r2 >= raw_min && r2 <= raw_max) return r2;
    }

    /* 最後の保険：クリップ */
    {
        double r3 = nearbyint(v);
        if (r3 < raw_min) r3 = raw_min;
        if (r3 > raw_max) r3 = raw_max;
        return r3;
    }
}

/* ---------------------------
   gain/offset 読み（無ければ 1/0）
   --------------------------- */
static void read_gain_offset(hid_t gwhat, double *gain, double *offset)
{
    *gain = 1.0;
    *offset = 0.0;

    double tmp;
    if (read_attr_double(gwhat, "gain", &tmp)) *gain = tmp;
    if (read_attr_double(gwhat, "offset", &tmp)) *offset = tmp;
}

/* ---------------------------
   nodata/undetect（raw値）を決める（旧処理）
   ※今回は raw=0 を無効値として固定するため未使用だが、残しておく
   --------------------------- */
static __attribute__((unused)) void decide_invalid_raw(
    hid_t gwhat,
    int is_u16,                 /* 1: uint16, 0: int16 */
    uint16_t *nodata_u16, uint16_t *undetect_u16,
    int16_t  *nodata_i16, int16_t  *undetect_i16)
{
    double gain, offset;
    read_gain_offset(gwhat, &gain, &offset);

    /* defaults */
    if (is_u16) {
        *nodata_u16 = 0;
        *undetect_u16 = 0;
    } else {
        *nodata_i16 = (int16_t)-32768;
        *undetect_i16 = (int16_t)-32767;
    }

    int has_nodata = (H5Aexists(gwhat, "nodata") > 0);
    int has_undetect = (H5Aexists(gwhat, "undetect") > 0);
    if (!has_nodata && !has_undetect) return;

    double raw_min = is_u16 ? 0.0 : -32768.0;
    double raw_max = is_u16 ? 65535.0 : 32767.0;

    double v;

    if (has_nodata && read_attr_double(gwhat, "nodata", &v)) {
        double r = to_raw_value(v, gain, offset, raw_min, raw_max);
        if (is_u16) *nodata_u16 = (uint16_t)r;
        else *nodata_i16 = (int16_t)r;
    }

    if (has_undetect && read_attr_double(gwhat, "undetect", &v)) {
        double r = to_raw_value(v, gain, offset, raw_min, raw_max);
        if (is_u16) *undetect_u16 = (uint16_t)r;
        else *undetect_i16 = (int16_t)r;
    }
}

/* =========================================================
   Median filter (仕様)
   - bin内点 : 近傍8 + 自分 = 9点
   - bin端点 : 近傍5 + 自分 = 6点
   - ray方向 : 周期
   - 無効値  : raw=0（観測値としてあり得ない）
   - 6点で 3:3 の場合は無効(0)に倒す
     -> totalが偶数なら invalid >= total/2 で無効
     -> totalが奇数なら invalid >  total/2 で無効
   ========================================================= */

static void sort_u16(uint16_t *a, int n)
{
    for (int i = 1; i < n; i++) {
        uint16_t key = a[i];
        int j = i - 1;
        while (j >= 0 && a[j] > key) { a[j + 1] = a[j]; j--; }
        a[j + 1] = key;
    }
}

static void sort_i16(int16_t *a, int n)
{
    for (int i = 1; i < n; i++) {
        int16_t key = a[i];
        int j = i - 1;
        while (j >= 0 && a[j] > key) { a[j + 1] = a[j]; j--; }
        a[j + 1] = key;
    }
}

/* median of non-zero values (n <= 9). even: average of middle two */
static uint16_t median_u16(uint16_t *v, int n)
{
    sort_u16(v, n);
    if (n & 1) return v[n / 2];
    uint32_t s = (uint32_t)v[n/2 - 1] + (uint32_t)v[n/2];
    return (uint16_t)((s + 1u) / 2u); /* round half up */
}

static int16_t median_i16(int16_t *v, int n)
{
    sort_i16(v, n);
    if (n & 1) return v[n / 2];
    double m = 0.5 * ((double)v[n/2 - 1] + (double)v[n/2]);
    long r = lround(m);
    if (r < -32768) r = -32768;
    if (r >  32767) r =  32767;
    return (int16_t)r;
}

static int should_be_invalid(int total, int invalid)
{
    if ((total & 1) == 0) return (invalid >= (total / 2)); /* tie -> invalid */
    return (invalid > (total / 2));                        /* majority only */
}

/* Collect {center + neighbors}:
   - inner bins: 9 points (8 neighbors + center)
   - edge bins : 6 points (5 neighbors + center)
   invalid raw is 0; we count zeros and collect non-zero values for median. */
static int gather_6_9_u16(const uint16_t *in, int r, int b, int nr, int nb,
                          uint16_t *vals /*size>=9*/, int *total, int *nzero)
{
    int rm1 = (r == 0) ? (nr - 1) : (r - 1);
    int rp1 = (r == nr - 1) ? 0 : (r + 1);
    int rays[3] = { rm1, r, rp1 };
    int bins[3] = { b - 1, b, b + 1 };

    int nt = 0, nz = 0, nnz = 0;

    /* center */
    {
        uint16_t x = in[r * nb + b];
        nt++;
        if (x == 0) nz++;
        else vals[nnz++] = x;
    }

    /* neighbors: exclude center, drop out-of-range bins */
    for (int ir = 0; ir < 3; ir++) {
        for (int ib = 0; ib < 3; ib++) {
            int rr = rays[ir];
            int bb = bins[ib];
            if (rr == r && bb == b) continue;
            if (bb < 0 || bb >= nb) continue;

            uint16_t x = in[rr * nb + bb];
            nt++;
            if (x == 0) nz++;
            else vals[nnz++] = x;
        }
    }

    *total = nt;  /* 9 or 6 */
    *nzero = nz;
    return nnz;
}

static int gather_6_9_i16(const int16_t *in, int r, int b, int nr, int nb,
                          int16_t *vals /*size>=9*/, int *total, int *nzero)
{
    int rm1 = (r == 0) ? (nr - 1) : (r - 1);
    int rp1 = (r == nr - 1) ? 0 : (r + 1);
    int rays[3] = { rm1, r, rp1 };
    int bins[3] = { b - 1, b, b + 1 };

    int nt = 0, nz = 0, nnz = 0;

    /* center */
    {
        int16_t x = in[r * nb + b];
        nt++;
        if (x == 0) nz++;
        else vals[nnz++] = x;
    }

    for (int ir = 0; ir < 3; ir++) {
        for (int ib = 0; ib < 3; ib++) {
            int rr = rays[ir];
            int bb = bins[ib];
            if (rr == r && bb == b) continue;
            if (bb < 0 || bb >= nb) continue;

            int16_t x = in[rr * nb + bb];
            nt++;
            if (x == 0) nz++;
            else vals[nnz++] = x;
        }
    }

    *total = nt;  /* 9 or 6 */
    *nzero = nz;
    return nnz;
}

/* ---------------------------
   近傍カウント（旧処理・未使用）
   --------------------------- */
static __attribute__((unused)) void neighbor_counts_u16(
    const uint16_t *in, int r, int b, int nr, int nb,
    uint16_t nodata, uint16_t undetect,
    int *valid, int *invalid, uint32_t *sum)
{
    *valid = 0; *invalid = 0; *sum = 0;

    int rm1 = (r == 0) ? (nr - 1) : (r - 1);
    int rp1 = (r == nr - 1) ? 0 : (r + 1);
    int rays[3] = { rm1, r, rp1 };
    int bins[3] = { b - 1, b, b + 1 };

    for (int ir = 0; ir < 3; ir++) {
        for (int ib = 0; ib < 3; ib++) {
            int rr = rays[ir];
            int bb = bins[ib];

            if (rr == r && bb == b) continue;
            if (bb < 0 || bb >= nb) continue;

            uint16_t v = in[rr * nb + bb];
            if (v == nodata || v == undetect) (*invalid)++;
            else { (*valid)++; (*sum) += v; }
        }
    }
}

static __attribute__((unused)) void neighbor_counts_i16(
    const int16_t *in, int r, int b, int nr, int nb,
    int16_t nodata, int16_t undetect,
    int *valid, int *invalid, int32_t *sum)
{
    *valid = 0; *invalid = 0; *sum = 0;

    int rm1 = (r == 0) ? (nr - 1) : (r - 1);
    int rp1 = (r == nr - 1) ? 0 : (r + 1);
    int rays[3] = { rm1, r, rp1 };
    int bins[3] = { b - 1, b, b + 1 };

    for (int ir = 0; ir < 3; ir++) {
        for (int ib = 0; ib < 3; ib++) {
            int rr = rays[ir];
            int bb = bins[ib];

            if (rr == r && bb == b) continue;
            if (bb < 0 || bb >= nb) continue;

            int16_t v = in[rr * nb + bb];
            if (v == nodata || v == undetect) (*invalid)++;
            else { (*valid)++; (*sum) += v; }
        }
    }
}

static __attribute__((unused)) void thresholds_for_bin(int b, int nb, int *th_invalid, int *th_valid)
{
    if (b == 0 || b == nb - 1) { *th_invalid = 4; *th_valid = 4; }
    else { *th_invalid = 7; *th_valid = 6; }
}

/* ---------------------------
   データ型判別（int16 / uint16）
   --------------------------- */
static int dataset_is_u16(hid_t dset, int *is_supported)
{
    *is_supported = 0;

    hid_t t = H5Dget_type(dset);
    if (t < 0) return 0;

    H5T_class_t cls = H5Tget_class(t);
    size_t sz = H5Tget_size(t);

    if (cls == H5T_INTEGER && sz == 2) {
        *is_supported = 1;
        H5T_sign_t sign = H5Tget_sign(t);
        H5Tclose(t);
        return (sign == H5T_SGN_NONE) ? 1 : 0;
    }

    H5Tclose(t);
    return 0;
}

/* ---------------------------
   1つの data を処理（uint16版）
   --------------------------- */
static void process_data_u16(hid_t file, const char *dataset, int data_index)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);

    char data_path[256], what_path[256];
    snprintf(data_path, sizeof(data_path), "%s/data%d/data", dataset, data_index);
    snprintf(what_path, sizeof(what_path), "%s/data%d/what", dataset, data_index);

    hid_t dset = H5Dopen(file, data_path, H5P_DEFAULT);
    if (dset < 0) { fprintf(stderr, "ERROR: cannot open %s\n", data_path); return; }

    hid_t gwhat = H5Gopen(file, what_path, H5P_DEFAULT);
    if (gwhat < 0) { fprintf(stderr, "ERROR: cannot open %s\n", what_path); H5Dclose(dset); return; }

    /* invalid raw is always 0 for this dataset (raw=0) */

    size_t n = (size_t)nr * (size_t)nb;
    uint16_t *in  = (uint16_t*)malloc(n * sizeof(uint16_t));
    uint16_t *out = (uint16_t*)malloc(n * sizeof(uint16_t));
    if (!in || !out) { fprintf(stderr, "ERROR: malloc failed\n"); exit(1); }

    if (H5Dread(dset, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, in) < 0) {
        fprintf(stderr, "ERROR: H5Dread failed (%s)\n", data_path);
        free(in); free(out);
        H5Gclose(gwhat); H5Dclose(dset);
        return;
    }

    for (int r = 0; r < nr; r++) {
        for (int b = 0; b < nb; b++) {
            uint16_t vals[9];
            int total, nzero;
            int nnz = gather_6_9_u16(in, r, b, nr, nb, vals, &total, &nzero);

            if (should_be_invalid(total, nzero)) {
                out[r * nb + b] = 0;
            } else {
                out[r * nb + b] = (nnz > 0) ? median_u16(vals, nnz) : 0;
            }
        }
    }

    if (H5Dwrite(dset, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, out) < 0) {
        fprintf(stderr, "ERROR: H5Dwrite failed (%s)\n", data_path);
    }

    free(in);
    free(out);
    H5Gclose(gwhat);
    H5Dclose(dset);
}

/* ---------------------------
   1つの data を処理（int16版）
   --------------------------- */
static void process_data_i16(hid_t file, const char *dataset, int data_index)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);

    char data_path[256], what_path[256];
    snprintf(data_path, sizeof(data_path), "%s/data%d/data", dataset, data_index);
    snprintf(what_path, sizeof(what_path), "%s/data%d/what", dataset, data_index);

    hid_t dset = H5Dopen(file, data_path, H5P_DEFAULT);
    if (dset < 0) { fprintf(stderr, "ERROR: cannot open %s\n", data_path); return; }

    hid_t gwhat = H5Gopen(file, what_path, H5P_DEFAULT);
    if (gwhat < 0) { fprintf(stderr, "ERROR: cannot open %s\n", what_path); H5Dclose(dset); return; }

    /* invalid raw is always 0 for this dataset (raw=0) */

    size_t n = (size_t)nr * (size_t)nb;
    int16_t *in  = (int16_t*)malloc(n * sizeof(int16_t));
    int16_t *out = (int16_t*)malloc(n * sizeof(int16_t));
    if (!in || !out) { fprintf(stderr, "ERROR: malloc failed\n"); exit(1); }

    if (H5Dread(dset, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, in) < 0) {
        fprintf(stderr, "ERROR: H5Dread failed (%s)\n", data_path);
        free(in); free(out);
        H5Gclose(gwhat); H5Dclose(dset);
        return;
    }

    for (int r = 0; r < nr; r++) {
        for (int b = 0; b < nb; b++) {
            int16_t vals[9];
            int total, nzero;
            int nnz = gather_6_9_i16(in, r, b, nr, nb, vals, &total, &nzero);

            if (should_be_invalid(total, nzero)) {
                out[r * nb + b] = 0;
            } else {
                out[r * nb + b] = (nnz > 0) ? median_i16(vals, nnz) : 0;
            }
        }
    }

    if (H5Dwrite(dset, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, out) < 0) {
        fprintf(stderr, "ERROR: H5Dwrite failed (%s)\n", data_path);
    }

    free(in);
    free(out);
    H5Gclose(gwhat);
    H5Dclose(dset);
}

/* ---------------------------
   1つの data を型判別して処理
   --------------------------- */
static void process_one_data(hid_t file, const char *dataset, int data_index)
{
    char data_path[256];
    snprintf(data_path, sizeof(data_path), "%s/data%d/data", dataset, data_index);

    hid_t dset = H5Dopen(file, data_path, H5P_DEFAULT);
    if (dset < 0) return;

    int supported = 0;
    int is_u16 = dataset_is_u16(dset, &supported);
    H5Dclose(dset);

    if (!supported) {
        fprintf(stderr, "skip %s (unsupported type)\n", data_path);
        return;
    }

    if (is_u16) process_data_u16(file, dataset, data_index);
    else        process_data_i16(file, dataset, data_index);
}

/* ---------------------------
   main
   --------------------------- */
int main(int argc, char **argv)
{
    suppress_hdf5_diag();

    if (argc < 3) {
        fprintf(stderr, "median ver 1.0\nusage: %s input.h5 DBZH [VRADH WRADH DBZX ...]\n", argv[0]);
        return 1;
    }

    hid_t file = H5Fopen(argv[1], H5F_ACC_RDWR, H5P_DEFAULT);
    if (file < 0) {
        fprintf(stderr, "ERROR: cannot open %s\n", argv[1]);
        return 1;
    }

    for (int d = 1; ; d++) {
        char dset[64];
        snprintf(dset, sizeof(dset), "/dataset%d", d);
        if (H5Lexists(file, dset, H5P_DEFAULT) <= 0) break;

        for (int m = 1; ; m++) {
            char what_path[128];
            snprintf(what_path, sizeof(what_path), "%s/data%d/what", dset, m);
            if (H5Lexists(file, what_path, H5P_DEFAULT) <= 0) break;

            hid_t gwhat = H5Gopen(file, what_path, H5P_DEFAULT);
            if (gwhat < 0) continue;

            char q[QLEN];
            int hasq = read_attr_string(gwhat, "quantity", q, sizeof(q));
            H5Gclose(gwhat);

            if (hasq && is_target_quantity(q, argc, argv)) {
                printf("Processing %s (%s/data%d)\n", q, dset, m);
                process_one_data(file, dset, m);
            }
        }
    }

    H5Fclose(file);
    return 0;
}
