#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include "hdf5.h"

#define QLEN 32
/* =====================================================
 * mdfill.c
 * Read .h5 file -> apply 3x3 Speckle Filter/Fill
 * to the specified data moments -> overwrite to the .h5
 *
 * Ver 1.0
 * 25.Dec.2025
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
   nodata/undetect（raw値）を決める

   優先順位：
   (1) 属性があれば読む（型が float/int でもOK）。
       読んだ値が raw として妥当なら raw。
       rawっぽくないなら physical と仮定して逆変換。
   (2) 属性が無ければデフォルト：
       uint16 -> 0
       int16  -> -32768, -32767
   --------------------------- */
static void decide_invalid_raw(
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

/* ---------------------------
   近傍カウント（仕様どおり）
   ray: 周期
   bin: 境界は存在しない側を除外 -> 5近傍/8近傍になる
   --------------------------- */
static void neighbor_counts_u16(
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

static void neighbor_counts_i16(
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

static void thresholds_for_bin(int b, int nb, int *th_invalid, int *th_valid)
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

    uint16_t nodata = 0, undetect = 0;
    int16_t dummy_i16a = 0, dummy_i16b = 0;
    decide_invalid_raw(gwhat, 1, &nodata, &undetect, &dummy_i16a, &dummy_i16b);

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
            uint16_t c = in[r * nb + b];

            int valid, invalid;
            uint32_t sum;
            neighbor_counts_u16(in, r, b, nr, nb, nodata, undetect, &valid, &invalid, &sum);

            int th_invalid, th_valid;
            thresholds_for_bin(b, nb, &th_invalid, &th_valid);

            if (!(c == nodata || c == undetect)) {
                out[r * nb + b] = (invalid >= th_invalid) ? nodata : c;
            } else {
                if (valid >= th_valid) {
                    out[r * nb + b] = (uint16_t)((sum + (uint32_t)(valid/2)) / (uint32_t)valid);
                } else {
                    out[r * nb + b] = c;
                }
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

    int16_t nodata = (int16_t)-32768, undetect = (int16_t)-32767;
    uint16_t dummy_u16a = 0, dummy_u16b = 0;
    decide_invalid_raw(gwhat, 0, &dummy_u16a, &dummy_u16b, &nodata, &undetect);

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
            int16_t c = in[r * nb + b];

            int valid, invalid;
            int32_t sum;
            neighbor_counts_i16(in, r, b, nr, nb, nodata, undetect, &valid, &invalid, &sum);

            int th_invalid, th_valid;
            thresholds_for_bin(b, nb, &th_invalid, &th_valid);

            if (!(c == nodata || c == undetect)) {
                out[r * nb + b] = (invalid >= th_invalid) ? nodata : c;
            } else {
                if (valid >= th_valid) {
                    int32_t v = (sum + (valid/2)) / valid;
                    if (v < -32768) v = -32768;
                    if (v > 32767)  v = 32767;
                    out[r * nb + b] = (int16_t)v;
                } else {
                    out[r * nb + b] = c;
                }
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
        fprintf(stderr, "mdfill ver 1.0\nusage: %s input.h5 DBZH [VRADH WRADH DBZX ...]\n", argv[0]);
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
