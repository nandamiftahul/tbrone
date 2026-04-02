#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include "hdf5.h"

#define QLEN 32
/* =====================================================
 * eprocess.c
 * eprocess <input hdf5 file> <data0> -corr -comp <data1> -conv <data2>
 * Read <input hdf5 file> -> 
 * -corr option : <data0> to be corrected with ZDR & RHOHV
 *       data0.corr = data0 + 0.5*ZDR - 10log(RHOHV)
 * -comp option : <data0> to be max-composited with <data1>
 * -conv option : processed <data0> to be copied to <data2>
 * ex) eprocess hdf5.h5 DBZX -corr -comp DBZH -conv DBZV
 *  => ZDR&RHOHV correction applied to DBZX -> DBZX = max(DBZX, DBZH)
 *     -> DBZV = DBZX
 *
 * Ver 1.0
 * 27.Dec.2025
 * =====================================================
 */ 

static void suppress_hdf5_diag(void)
{
    H5Eset_auto(H5E_DEFAULT, NULL, NULL);
}

static int read_attr_string(hid_t obj, const char *attr_name,
                            char *buf, size_t buflen)
{
    if (H5Aexists(obj, attr_name) <= 0) return 0;

    hid_t a = H5Aopen(obj, attr_name, H5P_DEFAULT);
    if (a < 0) return 0;

    hid_t t = H5Aget_type(a);
    if (t < 0) { H5Aclose(a); return 0; }

    H5T_class_t cls = H5Tget_class(t);
    int ok = 0;

    if (cls == H5T_STRING) {
        if (H5Tis_variable_str(t)) {
            char *s = NULL;
            hid_t m = H5Tcopy(H5T_C_S1);
            H5Tset_size(m, H5T_VARIABLE);
            if (H5Aread(a, m, &s) >= 0 && s) {
                strncpy(buf, s, buflen - 1);
                buf[buflen - 1] = '\0';
                free(s);
                ok = 1;
            }
            H5Tclose(m);
        } else {
            size_t sz = H5Tget_size(t);
            char *tmp = (char*)calloc(sz + 1, 1);
            if (tmp) {
                if (H5Aread(a, t, tmp) >= 0) {
                    tmp[sz] = '\0';
                    strncpy(buf, tmp, buflen - 1);
                    buf[buflen - 1] = '\0';
                    ok = 1;
                }
                free(tmp);
            }
        }
    }

    H5Tclose(t);
    H5Aclose(a);
    return ok;
}

static int read_attr_double(hid_t obj, const char *attr_name, double *out)
{
    if (H5Aexists(obj, attr_name) <= 0) return 0;

    hid_t a = H5Aopen(obj, attr_name, H5P_DEFAULT);
    if (a < 0) return 0;

    double v = 0.0;
    herr_t st = H5Aread(a, H5T_NATIVE_DOUBLE, &v);
    H5Aclose(a);

    if (st < 0) return 0;
    *out = v;
    return 1;
}

static void read_gain_offset(hid_t gwhat, double *gain, double *offset)
{
    *gain = 1.0;
    *offset = 0.0;

    double tmp;
    if (read_attr_double(gwhat, "gain", &tmp))   *gain = tmp;
    if (read_attr_double(gwhat, "offset", &tmp)) *offset = tmp;
}

static void read_dims(hid_t file, const char *dataset, int *nrays, int *nbins)
{
    char path[128];
    snprintf(path, sizeof(path), "%s/where", dataset);

    hid_t g = H5Gopen(file, path, H5P_DEFAULT);
    if (g < 0) { fprintf(stderr, "ERROR: cannot open %s\n", path); exit(1); }

    if (H5Aexists(g, "nrays") <= 0 || H5Aexists(g, "nbins") <= 0) {
        fprintf(stderr, "ERROR: nrays/nbins not found in %s\n", path);
        H5Gclose(g);
        exit(1);
    }

    hid_t a = H5Aopen(g, "nrays", H5P_DEFAULT);
    if (a < 0) { fprintf(stderr, "ERROR: cannot open nrays\n"); exit(1); }
    if (H5Aread(a, H5T_NATIVE_INT, nrays) < 0) { fprintf(stderr, "ERROR: cannot read nrays\n"); exit(1); }
    H5Aclose(a);

    a = H5Aopen(g, "nbins", H5P_DEFAULT);
    if (a < 0) { fprintf(stderr, "ERROR: cannot open nbins\n"); exit(1); }
    if (H5Aread(a, H5T_NATIVE_INT, nbins) < 0) { fprintf(stderr, "ERROR: cannot read nbins\n"); exit(1); }
    H5Aclose(a);

    H5Gclose(g);
}

static double to_raw_value(double v, double gain, double offset,
                           double raw_min, double raw_max)
{
    if (!isfinite(v)) return NAN;

    /* raw として解釈できるか */
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

    /* 範囲外はクリップ */
    if (gain != 0.0) {
        double r3 = nearbyint((v - offset) / gain);
        if (r3 < raw_min) r3 = raw_min;
        if (r3 > raw_max) r3 = raw_max;
        return r3;
    }
    return NAN;
}

static int dataset_is_u16(hid_t file, const char *dataset, int data_index)
{
    char data_path[256];
    snprintf(data_path, sizeof(data_path), "%s/data%d/data", dataset, data_index);

    hid_t dset = H5Dopen(file, data_path, H5P_DEFAULT);
    if (dset < 0) { fprintf(stderr, "ERROR: cannot open %s\n", data_path); exit(1); }

    hid_t t = H5Dget_type(dset);
    if (t < 0) { fprintf(stderr, "ERROR: cannot get type %s\n", data_path); exit(1); }

    H5T_class_t cls = H5Tget_class(t);
    size_t sz = H5Tget_size(t);
    H5T_sign_t sign = H5Tget_sign(t);

    H5Tclose(t);
    H5Dclose(dset);

    if (cls != H5T_INTEGER) {
        fprintf(stderr, "ERROR: %s is not integer type\n", data_path);
        exit(1);
    }
    if (sz != 2) {
        fprintf(stderr, "ERROR: %s integer size is not 2 bytes\n", data_path);
        exit(1);
    }

    return (sign == H5T_SGN_NONE);
}

static int find_data_index_in_dataset(hid_t file, const char *dataset, const char *quantity)
{
    for (int m = 1; ; m++) {
        char what_path[128];
        snprintf(what_path, sizeof(what_path), "%s/data%d/what", dataset, m);
        if (H5Lexists(file, what_path, H5P_DEFAULT) <= 0) break;

        hid_t gwhat = H5Gopen(file, what_path, H5P_DEFAULT);
        if (gwhat < 0) continue;

        char q[QLEN] = {0};
        int hasq = read_attr_string(gwhat, "quantity", q, sizeof(q));
        H5Gclose(gwhat);

        if (hasq && strcmp(q, quantity) == 0) return m;
    }
    return 0;
}

/* =========================
   -corr / -comp (uint16)
   invalid raw is always 0
   ========================= */
static void corr_u16(hid_t file, const char *dataset,
                     int idx_t, int idx_zdr, int idx_rhohv)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);
    size_t n = (size_t)nr * (size_t)nb;

    char p_t[256], p_z[256], p_r[256];
    char w_t[256], w_z[256], w_r[256];
    snprintf(p_t, sizeof(p_t), "%s/data%d/data", dataset, idx_t);
    snprintf(p_z, sizeof(p_z), "%s/data%d/data", dataset, idx_zdr);
    snprintf(p_r, sizeof(p_r), "%s/data%d/data", dataset, idx_rhohv);
    snprintf(w_t, sizeof(w_t), "%s/data%d/what", dataset, idx_t);
    snprintf(w_z, sizeof(w_z), "%s/data%d/what", dataset, idx_zdr);
    snprintf(w_r, sizeof(w_r), "%s/data%d/what", dataset, idx_rhohv);

    hid_t d_t = H5Dopen(file, p_t, H5P_DEFAULT);
    hid_t d_z = H5Dopen(file, p_z, H5P_DEFAULT);
    hid_t d_r = H5Dopen(file, p_r, H5P_DEFAULT);
    if (d_t<0 || d_z<0 || d_r<0) { fprintf(stderr,"ERROR: cannot open datasets for -corr\n"); goto done0; }

    hid_t g_t = H5Gopen(file, w_t, H5P_DEFAULT);
    hid_t g_z = H5Gopen(file, w_z, H5P_DEFAULT);
    hid_t g_r = H5Gopen(file, w_r, H5P_DEFAULT);
    if (g_t<0 || g_z<0 || g_r<0) { fprintf(stderr,"ERROR: cannot open what groups for -corr\n"); goto done1; }

    double gt, ot, gz, oz, gr, orh;
    read_gain_offset(g_t, &gt, &ot);
    read_gain_offset(g_z, &gz, &oz);
    read_gain_offset(g_r, &gr, &orh);

    uint16_t *rt = (uint16_t*)malloc(n*sizeof(uint16_t));
    uint16_t *rz = (uint16_t*)malloc(n*sizeof(uint16_t));
    uint16_t *rr = (uint16_t*)malloc(n*sizeof(uint16_t));
    uint16_t *out= (uint16_t*)malloc(n*sizeof(uint16_t));
    if(!rt||!rz||!rr||!out){ fprintf(stderr,"ERROR: malloc failed\n"); exit(1); }

    if (H5Dread(d_t, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rt) < 0 ||
        H5Dread(d_z, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rz) < 0 ||
        H5Dread(d_r, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rr) < 0) {
        fprintf(stderr, "ERROR: H5Dread failed (-corr)\n");
        free(rt); free(rz); free(rr); free(out);
        goto done2;
    }

    const double raw_min = 0.0, raw_max = 65535.0;

    for (size_t i = 0; i < n; i++) {
        /* 新仕様：
           - targetが無効(0)なら無効のまま
           - ZDRまたはRHOHVが無効なら、targetは元の値のまま（補正しない）
           - 3つとも有効なら補正
        */
        if (rt[i] == 0) { out[i] = 0; continue; }
        if (rz[i] == 0 || rr[i] == 0) { out[i] = rt[i]; continue; }

        double pt = (double)rt[i]*gt + ot;
        double pz = (double)rz[i]*gz + oz;
        double pr = (double)rr[i]*gr + orh;

        if (!isfinite(pr) || pr <= 0.0) { out[i] = rt[i]; continue; } /* 計算不能なら保持 */

        double pc = pt + 0.5*pz - 10.0*log10(pr);

        double raw = to_raw_value(pc, gt, ot, raw_min, raw_max);
        if (!isfinite(raw)) out[i] = rt[i]; /* 念のため：変換不能なら保持 */
        else out[i] = (uint16_t)raw;
    }

    if (H5Dwrite(d_t, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, out) < 0) {
        fprintf(stderr, "ERROR: H5Dwrite failed (-corr) %s\n", p_t);
    }

    free(rt); free(rz); free(rr); free(out);

done2:
    H5Gclose(g_t); H5Gclose(g_z); H5Gclose(g_r);
done1:
    if (d_t>=0) H5Dclose(d_t);
    if (d_z>=0) H5Dclose(d_z);
    if (d_r>=0) H5Dclose(d_r);
done0:
    return;
}

static void comp_u16(hid_t file, const char *dataset, int idx_t, int idx_c)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);
    size_t n = (size_t)nr * (size_t)nb;

    char p_t[256], p_c[256];
    char w_t[256], w_c[256];
    snprintf(p_t, sizeof(p_t), "%s/data%d/data", dataset, idx_t);
    snprintf(p_c, sizeof(p_c), "%s/data%d/data", dataset, idx_c);
    snprintf(w_t, sizeof(w_t), "%s/data%d/what", dataset, idx_t);
    snprintf(w_c, sizeof(w_c), "%s/data%d/what", dataset, idx_c);

    hid_t d_t = H5Dopen(file, p_t, H5P_DEFAULT);
    hid_t d_c = H5Dopen(file, p_c, H5P_DEFAULT);
    if (d_t<0 || d_c<0) { fprintf(stderr,"ERROR: cannot open datasets for -comp\n"); goto done0; }

    hid_t g_t = H5Gopen(file, w_t, H5P_DEFAULT);
    hid_t g_c = H5Gopen(file, w_c, H5P_DEFAULT);
    if (g_t<0 || g_c<0) { fprintf(stderr,"ERROR: cannot open what groups for -comp\n"); goto done1; }

    double gt, ot, gc, oc;
    read_gain_offset(g_t, &gt, &ot);
    read_gain_offset(g_c, &gc, &oc);

    uint16_t *rt = (uint16_t*)malloc(n*sizeof(uint16_t));
    uint16_t *rc = (uint16_t*)malloc(n*sizeof(uint16_t));
    uint16_t *out= (uint16_t*)malloc(n*sizeof(uint16_t));
    if(!rt||!rc||!out){ fprintf(stderr,"ERROR: malloc failed\n"); exit(1); }

    if (H5Dread(d_t, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rt) < 0 ||
        H5Dread(d_c, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rc) < 0) {
        fprintf(stderr, "ERROR: H5Dread failed (-comp)\n");
        free(rt); free(rc); free(out);
        goto done2;
    }

    const double raw_min = 0.0, raw_max = 65535.0;

    for (size_t i = 0; i < n; i++) {
        int vt = (rt[i] != 0);
        int vc = (rc[i] != 0);

        if (!vt && !vc) { out[i] = 0; continue; }

        double pt = vt ? ((double)rt[i]*gt + ot) : 0.0;
        double pc = vc ? ((double)rc[i]*gc + oc) : 0.0;

        double chosen;
        if (vt && vc) chosen = (pt >= pc) ? pt : pc;
        else if (vt)  chosen = pt;
        else          chosen = pc;

        double raw = to_raw_value(chosen, gt, ot, raw_min, raw_max);
        if (!isfinite(raw)) out[i] = 0;
        else out[i] = (uint16_t)raw;
    }

    if (H5Dwrite(d_t, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, out) < 0) {
        fprintf(stderr, "ERROR: H5Dwrite failed (-comp) %s\n", p_t);
    }

    free(rt); free(rc); free(out);

done2:
    H5Gclose(g_t); H5Gclose(g_c);
done1:
    if (d_t>=0) H5Dclose(d_t);
    if (d_c>=0) H5Dclose(d_c);
done0:
    return;
}

/* =========================
   -corr / -comp (int16)
   invalid raw is always 0
   ========================= */
static void corr_i16(hid_t file, const char *dataset,
                     int idx_t, int idx_zdr, int idx_rhohv)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);
    size_t n = (size_t)nr * (size_t)nb;

    char p_t[256], p_z[256], p_r[256];
    char w_t[256], w_z[256], w_r[256];
    snprintf(p_t, sizeof(p_t), "%s/data%d/data", dataset, idx_t);
    snprintf(p_z, sizeof(p_z), "%s/data%d/data", dataset, idx_zdr);
    snprintf(p_r, sizeof(p_r), "%s/data%d/data", dataset, idx_rhohv);
    snprintf(w_t, sizeof(w_t), "%s/data%d/what", dataset, idx_t);
    snprintf(w_z, sizeof(w_z), "%s/data%d/what", dataset, idx_zdr);
    snprintf(w_r, sizeof(w_r), "%s/data%d/what", dataset, idx_rhohv);

    hid_t d_t = H5Dopen(file, p_t, H5P_DEFAULT);
    hid_t d_z = H5Dopen(file, p_z, H5P_DEFAULT);
    hid_t d_r = H5Dopen(file, p_r, H5P_DEFAULT);
    if (d_t<0 || d_z<0 || d_r<0) { fprintf(stderr,"ERROR: cannot open datasets for -corr\n"); goto done0; }

    hid_t g_t = H5Gopen(file, w_t, H5P_DEFAULT);
    hid_t g_z = H5Gopen(file, w_z, H5P_DEFAULT);
    hid_t g_r = H5Gopen(file, w_r, H5P_DEFAULT);
    if (g_t<0 || g_z<0 || g_r<0) { fprintf(stderr,"ERROR: cannot open what groups for -corr\n"); goto done1; }

    double gt, ot, gz, oz, gr, orh;
    read_gain_offset(g_t, &gt, &ot);
    read_gain_offset(g_z, &gz, &oz);
    read_gain_offset(g_r, &gr, &orh);

    int16_t *rt = (int16_t*)malloc(n*sizeof(int16_t));
    int16_t *rz = (int16_t*)malloc(n*sizeof(int16_t));
    int16_t *rr = (int16_t*)malloc(n*sizeof(int16_t));
    int16_t *out= (int16_t*)malloc(n*sizeof(int16_t));
    if(!rt||!rz||!rr||!out){ fprintf(stderr,"ERROR: malloc failed\n"); exit(1); }

    if (H5Dread(d_t, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rt) < 0 ||
        H5Dread(d_z, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rz) < 0 ||
        H5Dread(d_r, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rr) < 0) {
        fprintf(stderr, "ERROR: H5Dread failed (-corr)\n");
        free(rt); free(rz); free(rr); free(out);
        goto done2;
    }

    const double raw_min = -32768.0, raw_max = 32767.0;

    for (size_t i = 0; i < n; i++) {
        if (rt[i] == 0) { out[i] = 0; continue; }
        if (rz[i] == 0 || rr[i] == 0) { out[i] = rt[i]; continue; }

        double pt = (double)rt[i]*gt + ot;
        double pz = (double)rz[i]*gz + oz;
        double pr = (double)rr[i]*gr + orh;

        if (!isfinite(pr) || pr <= 0.0) { out[i] = rt[i]; continue; }

        double pc = pt + 0.5*pz - 10.0*log10(pr);

        double raw = to_raw_value(pc, gt, ot, raw_min, raw_max);
        if (!isfinite(raw)) out[i] = rt[i];
        else out[i] = (int16_t)raw;
    }

    if (H5Dwrite(d_t, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, out) < 0) {
        fprintf(stderr, "ERROR: H5Dwrite failed (-corr) %s\n", p_t);
    }

    free(rt); free(rz); free(rr); free(out);

done2:
    H5Gclose(g_t); H5Gclose(g_z); H5Gclose(g_r);
done1:
    if (d_t>=0) H5Dclose(d_t);
    if (d_z>=0) H5Dclose(d_z);
    if (d_r>=0) H5Dclose(d_r);
done0:
    return;
}

static void comp_i16(hid_t file, const char *dataset, int idx_t, int idx_c)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);
    size_t n = (size_t)nr * (size_t)nb;

    char p_t[256], p_c[256];
    char w_t[256], w_c[256];
    snprintf(p_t, sizeof(p_t), "%s/data%d/data", dataset, idx_t);
    snprintf(p_c, sizeof(p_c), "%s/data%d/data", dataset, idx_c);
    snprintf(w_t, sizeof(w_t), "%s/data%d/what", dataset, idx_t);
    snprintf(w_c, sizeof(w_c), "%s/data%d/what", dataset, idx_c);

    hid_t d_t = H5Dopen(file, p_t, H5P_DEFAULT);
    hid_t d_c = H5Dopen(file, p_c, H5P_DEFAULT);
    if (d_t<0 || d_c<0) { fprintf(stderr,"ERROR: cannot open datasets for -comp\n"); goto done0; }

    hid_t g_t = H5Gopen(file, w_t, H5P_DEFAULT);
    hid_t g_c = H5Gopen(file, w_c, H5P_DEFAULT);
    if (g_t<0 || g_c<0) { fprintf(stderr,"ERROR: cannot open what groups for -comp\n"); goto done1; }

    double gt, ot, gc, oc;
    read_gain_offset(g_t, &gt, &ot);
    read_gain_offset(g_c, &gc, &oc);

    int16_t *rt = (int16_t*)malloc(n*sizeof(int16_t));
    int16_t *rc = (int16_t*)malloc(n*sizeof(int16_t));
    int16_t *out= (int16_t*)malloc(n*sizeof(int16_t));
    if(!rt||!rc||!out){ fprintf(stderr,"ERROR: malloc failed\n"); exit(1); }

    if (H5Dread(d_t, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rt) < 0 ||
        H5Dread(d_c, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, rc) < 0) {
        fprintf(stderr, "ERROR: H5Dread failed (-comp)\n");
        free(rt); free(rc); free(out);
        goto done2;
    }

    const double raw_min = -32768.0, raw_max = 32767.0;

    for (size_t i = 0; i < n; i++) {
        int vt = (rt[i] != 0);
        int vc = (rc[i] != 0);

        if (!vt && !vc) { out[i] = 0; continue; }

        double pt = vt ? ((double)rt[i]*gt + ot) : 0.0;
        double pc = vc ? ((double)rc[i]*gc + oc) : 0.0;

        double chosen;
        if (vt && vc) chosen = (pt >= pc) ? pt : pc;
        else if (vt)  chosen = pt;
        else          chosen = pc;

        double raw = to_raw_value(chosen, gt, ot, raw_min, raw_max);
        if (!isfinite(raw)) out[i] = 0;
        else out[i] = (int16_t)raw;
    }

    if (H5Dwrite(d_t, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, out) < 0) {
        fprintf(stderr, "ERROR: H5Dwrite failed (-comp) %s\n", p_t);
    }

    free(rt); free(rc); free(out);

done2:
    H5Gclose(g_t); H5Gclose(g_c);
done1:
    if (d_t>=0) H5Dclose(d_t);
    if (d_c>=0) H5Dclose(d_c);
done0:
    return;
}

/* ---- -conv（前回提示のまま） ---- */
static int types_equal(hid_t t1, hid_t t2)
{
    hid_t n1 = H5Tget_native_type(t1, H5T_DIR_ASCEND);
    hid_t n2 = H5Tget_native_type(t2, H5T_DIR_ASCEND);
    int eq = (H5Tequal(n1, n2) > 0);
    H5Tclose(n1);
    H5Tclose(n2);
    return eq;
}

static int recreate_dataset_with_type(hid_t file,
                                      const char *dst_path,
                                      hid_t new_type,
                                      hid_t space,
                                      const void *buf,
                                      hid_t mem_type)
{
    char tmp_path[512];
    snprintf(tmp_path, sizeof(tmp_path), "%s__tmp", dst_path);

    if (H5Lexists(file, tmp_path, H5P_DEFAULT) > 0) {
        H5Ldelete(file, tmp_path, H5P_DEFAULT);
    }

    hid_t dtmp = H5Dcreate2(file, tmp_path, new_type, space, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    if (dtmp < 0) {
        fprintf(stderr, "ERROR: failed to create tmp dataset %s\n", tmp_path);
        return 0;
    }
    if (H5Dwrite(dtmp, mem_type, H5S_ALL, H5S_ALL, H5P_DEFAULT, buf) < 0) {
        fprintf(stderr, "ERROR: failed to write tmp dataset %s\n", tmp_path);
        H5Dclose(dtmp);
        return 0;
    }
    H5Dclose(dtmp);

    if (H5Ldelete(file, dst_path, H5P_DEFAULT) < 0) {
        fprintf(stderr, "ERROR: failed to delete old dataset %s\n", dst_path);
        return 0;
    }
    if (H5Lmove(file, tmp_path, file, dst_path, H5P_DEFAULT, H5P_DEFAULT) < 0) {
        fprintf(stderr, "ERROR: failed to rename tmp dataset to %s\n", dst_path);
        return 0;
    }
    return 1;
}

static void conv_copy(hid_t file, const char *dataset,
                      int idx_src, int idx_dst)
{
    int nr, nb;
    read_dims(file, dataset, &nr, &nb);
    size_t n = (size_t)nr * (size_t)nb;

    char p_s[256], p_d[256];
    snprintf(p_s, sizeof(p_s), "%s/data%d/data", dataset, idx_src);
    snprintf(p_d, sizeof(p_d), "%s/data%d/data", dataset, idx_dst);

    hid_t ds = H5Dopen(file, p_s, H5P_DEFAULT);
    hid_t dd = H5Dopen(file, p_d, H5P_DEFAULT);
    if (ds < 0 || dd < 0) {
        fprintf(stderr, "ERROR: cannot open datasets for -conv\n");
        if (ds>=0) H5Dclose(ds);
        if (dd>=0) H5Dclose(dd);
        return;
    }

    hid_t ts = H5Dget_type(ds);
    hid_t td = H5Dget_type(dd);
    hid_t space_d = H5Dget_space(dd);

    {
        int nd = H5Sget_simple_extent_ndims(space_d);
        if (nd != 2) {
            fprintf(stderr, "ERROR: dst dataspace ndims != 2 (%s)\n", p_d);
            goto done;
        }
        hsize_t dims[2];
        H5Sget_simple_extent_dims(space_d, dims, NULL);
        if ((size_t)dims[0] != (size_t)nr || (size_t)dims[1] != (size_t)nb) {
            fprintf(stderr, "ERROR: dst shape mismatch (%s)\n", p_d);
            goto done;
        }
    }

    if (H5Tget_class(ts) != H5T_INTEGER || H5Tget_size(ts) != 2) {
        fprintf(stderr, "ERROR: src type unsupported (need 2-byte integer)\n");
        goto done;
    }

    int src_u16 = (H5Tget_sign(ts) == H5T_SGN_NONE);
    if (src_u16) {
        uint16_t *buf = (uint16_t*)malloc(n*sizeof(uint16_t));
        if (!buf) { fprintf(stderr, "ERROR: malloc failed\n"); exit(1); }
        if (H5Dread(ds, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, buf) < 0) {
            fprintf(stderr, "ERROR: H5Dread failed (-conv) %s\n", p_s);
            free(buf);
            goto done;
        }

        if (types_equal(ts, td)) {
            if (H5Dwrite(dd, H5T_NATIVE_USHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, buf) < 0) {
                fprintf(stderr, "ERROR: H5Dwrite failed (-conv) %s\n", p_d);
            }
        } else {
            if (!recreate_dataset_with_type(file, p_d, ts, space_d, buf, H5T_NATIVE_USHORT)) {
                fprintf(stderr, "ERROR: recreate dst dataset failed (-conv) %s\n", p_d);
            }
        }
        free(buf);
    } else {
        int16_t *buf = (int16_t*)malloc(n*sizeof(int16_t));
        if (!buf) { fprintf(stderr, "ERROR: malloc failed\n"); exit(1); }
        if (H5Dread(ds, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, buf) < 0) {
            fprintf(stderr, "ERROR: H5Dread failed (-conv) %s\n", p_s);
            free(buf);
            goto done;
        }

        if (types_equal(ts, td)) {
            if (H5Dwrite(dd, H5T_NATIVE_SHORT, H5S_ALL, H5S_ALL, H5P_DEFAULT, buf) < 0) {
                fprintf(stderr, "ERROR: H5Dwrite failed (-conv) %s\n", p_d);
            }
        } else {
            if (!recreate_dataset_with_type(file, p_d, ts, space_d, buf, H5T_NATIVE_SHORT)) {
                fprintf(stderr, "ERROR: recreate dst dataset failed (-conv) %s\n", p_d);
            }
        }
        free(buf);
    }

done:
    if (space_d>=0) H5Sclose(space_d);
    if (ts>=0) H5Tclose(ts);
    if (td>=0) H5Tclose(td);
    H5Dclose(ds);
    H5Dclose(dd);
}

/* ---- args/dispatch ---- */
typedef struct {
    const char *file;
    const char *target_qty;
    int do_corr;
    int do_comp;
    const char *comp_qty;
    int do_conv;
    const char *conv_qty;
} Args;

static void usage(const char *prog)
{
    fprintf(stderr,
        "eprocess ver 1.0\n"
        "usage: %s <input.h5> <TARGET_Q0> [-corr] [-comp <Q1>] [-conv <Q2>]\n(ex) eprocess hdf5.h5 DBZX -corr -comp DBZH -conv DBZV\n",
        prog);
}

static int parse_args(int argc, char **argv, Args *a)
{
    if (argc < 3) return 0;
    memset(a, 0, sizeof(*a));
    a->file = argv[1];
    a->target_qty = argv[2];

    for (int i = 3; i < argc; i++) {
        if (strcmp(argv[i], "-corr") == 0) {
            a->do_corr = 1;
        } else if (strcmp(argv[i], "-comp") == 0) {
            if (i + 1 >= argc) return 0;
            a->do_comp = 1;
            a->comp_qty = argv[++i];
        } else if (strcmp(argv[i], "-conv") == 0) {
            if (i + 1 >= argc) return 0;
            a->do_conv = 1;
            a->conv_qty = argv[++i];
        } else {
            fprintf(stderr, "Unknown option: %s\n", argv[i]);
            return 0;
        }
    }
    return 1;
}

static void process_one_dataset(hid_t file, const char *dset, const Args *args)
{
    int idx_t = find_data_index_in_dataset(file, dset, args->target_qty);
    if (idx_t == 0) return;

    int is_u16 = dataset_is_u16(file, dset, idx_t);

    if (args->do_corr) {
        int idx_z = find_data_index_in_dataset(file, dset, "ZDR");
        int idx_r = find_data_index_in_dataset(file, dset, "RHOHV");
        if (idx_z == 0 || idx_r == 0) {
            fprintf(stderr, "WARN: %s missing ZDR/RHOHV -> skip -corr\n", dset);
        } else {
            printf("Processing -corr: %s %s(data%d) using ZDR(data%d), RHOHV(data%d)\n",
                   dset, args->target_qty, idx_t, idx_z, idx_r);
            if (is_u16) corr_u16(file, dset, idx_t, idx_z, idx_r);
            else        corr_i16(file, dset, idx_t, idx_z, idx_r);
        }
    }

    if (args->do_comp) {
        int idx_c = find_data_index_in_dataset(file, dset, args->comp_qty);
        if (idx_c == 0) {
            fprintf(stderr, "WARN: %s missing comp %s -> skip -comp\n", dset, args->comp_qty);
        } else {
            printf("Processing -comp: %s %s(data%d) with %s(data%d)\n",
                   dset, args->target_qty, idx_t, args->comp_qty, idx_c);
            if (is_u16) comp_u16(file, dset, idx_t, idx_c);
            else        comp_i16(file, dset, idx_t, idx_c);
        }
    }

    if (args->do_conv) {
        int idx_d = find_data_index_in_dataset(file, dset, args->conv_qty);
        if (idx_d == 0) {
            fprintf(stderr, "WARN: %s missing conv %s -> skip -conv\n", dset, args->conv_qty);
        } else {
            printf("Processing -conv: %s copy %s(data%d) -> %s(data%d)\n",
                   dset, args->target_qty, idx_t, args->conv_qty, idx_d);
            conv_copy(file, dset, idx_t, idx_d);
        }
    }
}

int main(int argc, char **argv)
{
    suppress_hdf5_diag();

    Args args;
    if (!parse_args(argc, argv, &args)) {
        usage(argv[0]);
        return 1;
    }

    if (!args.do_corr && !args.do_comp && !args.do_conv) {
        return 0;
    }

    hid_t file = H5Fopen(args.file, H5F_ACC_RDWR, H5P_DEFAULT);
    if (file < 0) {
        fprintf(stderr, "ERROR: cannot open %s\n", args.file);
        return 2;
    }

    for (int n = 1; ; n++) {
        char dset[64];
        snprintf(dset, sizeof(dset), "/dataset%d", n);
        if (H5Lexists(file, dset, H5P_DEFAULT) <= 0) break;
        process_one_dataset(file, dset, &args);
    }

    H5Fclose(file);
    return 0;
}
