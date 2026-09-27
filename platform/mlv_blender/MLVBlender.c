#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <inttypes.h>
#include <math.h>

#include "MLVBlender.h"
#include "../../src/dng/dng.h"

int MLVBlenderInclusiveFrameRange(uint64_t frame_count,
                                  uint32_t * frame_start,
                                  uint32_t * frame_end)
{
    if (!frame_start || !frame_end || frame_count == 0 || frame_count > UINT32_MAX)
        return 0;
    *frame_start = 0;
    *frame_end = (uint32_t)(frame_count - 1u);
    return 1;
}

uint16_t MLVBlenderQuantize14(float normalized, int black_level)
{
    const float maximum = 16383.0f;
    if (!isfinite(normalized)) normalized = 0.0f;
    if (normalized < 0.0f) normalized = 0.0f;
    if (normalized > 1.0f) normalized = 1.0f;
    if (black_level < 0) black_level = 0;
    if (black_level > 16382) black_level = 16382;
    float result = normalized * (maximum - (float)black_level) + (float)black_level;
    if (result < 0.0f) result = 0.0f;
    if (result > maximum) result = maximum;
    return (uint16_t)result;
}

static uint64_t file_set_pos(FILE *stream, uint64_t offset, int whence)
{
#if defined(__WIN32)
    return fseeko64(stream, offset, whence);
#else
    return fseek(stream, offset, whence);
#endif
}

void init_MLVBlender(MLVBlender_t * Blender)
{
    Blender->mode = 0;
    Blender->num_mlvs = 0;
    Blender->mlvs = malloc(sizeof(MLVBlender_mlv_t));
    Blender->blended_output = malloc(sizeof(uint16_t));

    return;
}

void free_MLVBlender(MLVBlender_t * Blender)
{
    for (int i = 0; i < Blender->num_mlvs; ++i)
    {
        MLVBlender_mlv_t * mlv = Blender->mlvs + i;
        freeMlvObject(mlv->mlv);
        free(mlv->file_name);
    }
    free(Blender->mlvs);

    free(Blender->blended_output);
}

void MLVBlenderAddMLV(MLVBlender_t * Blender, const char * MLVPath)
{
    if (!Blender || !MLVPath || Blender->num_mlvs < 0
        || (size_t)Blender->num_mlvs >= SIZE_MAX / sizeof(MLVBlender_mlv_t) - 1u)
        return;

    MLVBlender_mlv_t candidate = { 0 };
    MLVBlender_mlv_t * mlv = &candidate;

    /* file name */
    mlv->file_name = malloc(strlen(MLVPath)+1);
    if (!mlv->file_name) return;
    strcpy(mlv->file_name, MLVPath);

    /* offsets and crop */
    mlv->offset_x = 0;
    mlv->offset_y = 0;

    mlv->crop_left = 0;
    mlv->crop_right = 0;
    mlv->crop_bottom = 0;
    mlv->crop_top = 0;

    mlv->feather_left = 0;
    mlv->feather_right = 0;
    mlv->feather_bottom = 0;
    mlv->feather_top = 0;

    mlv->visible = 1;

    mlv->difference_blending = 0;

    mlv->exposure = 1.0;

    /* mlv object */
    mlv->mlv = initMlvObjectWithClip((char *)MLVPath, MLV_OPEN_FULL, NULL, NULL);
    if (!mlv->mlv) {
        free(mlv->file_name);
        return;
    }
    disableMlvCaching(mlv->mlv);
    printMlvInfo(mlv->mlv);

    mlv->num_frames = getMlvFrames(mlv->mlv);
    mlv->width = getMlvWidth(mlv->mlv);
    mlv->height = getMlvHeight(mlv->mlv);

    MLVBlender_mlv_t * replacement = realloc(
        Blender->mlvs,
        ((size_t)Blender->num_mlvs + 1u) * sizeof(MLVBlender_mlv_t));
    if (!replacement) {
        freeMlvObject(mlv->mlv);
        free(mlv->file_name);
        return;
    }
    Blender->mlvs = replacement;
    Blender->mlvs[Blender->num_mlvs] = candidate;
    Blender->num_mlvs++;


    /**************************** AUTO POSITIONING ****************************/

    /* Only do positioning if clip isn't first and has same resolution as first */
    // MLVBlender_mlv_t * mlv0 = Blender->mlvs;

    //     printf("cropPosY = %i\n", mlv->mlv->RAWI.raw_info.active_area.x1);
    //     printf("cropPosX = %i\n", mlv->mlv->RAWI.raw_info.active_area.y1);
    // if (Blender->num_mlvs > 1 && mlv->width == mlv0->width && mlv->height == mlv0->height)
    // {
    // }
}

const char * MLVBlenderGetMLVFileName(MLVBlender_t * Blender, int Index)
{
    return Blender->mlvs[Index].file_name;
}

int MLVBlenderGetNumMLVs(MLVBlender_t * Blender)
{
    return Blender->num_mlvs;
}

void MLVBlenderBlend(MLVBlender_t * Blender, uint64_t FrameIndex)
{
    /************************* CALCULATE RESULT SIZE **************************/

    if (!Blender || Blender->num_mlvs <= 0 || !Blender->mlvs) return;

    int64_t max_x = INT64_MIN;
    int64_t max_y = INT64_MIN;
    int64_t min_x = INT64_MAX;
    int64_t min_y = INT64_MAX;

    for (int i = 0; i < Blender->num_mlvs; ++i)
    {
        printf(" * Blending image from video %i\n", i);
        MLVBlender_mlv_t * mlv = Blender->mlvs + i;

        if (!mlv->mlv || mlv->width <= 0 || mlv->height <= 0
            || mlv->crop_left < 0 || mlv->crop_right < 0
            || mlv->crop_top < 0 || mlv->crop_bottom < 0
            || mlv->crop_left >= mlv->width - mlv->crop_right
            || mlv->crop_bottom >= mlv->height - mlv->crop_top
            || mlv->feather_left < 0 || mlv->feather_right < 0
            || mlv->feather_top < 0 || mlv->feather_bottom < 0) {
            free(Blender->blended_output);
            Blender->blended_output = NULL;
            Blender->output_width = 0;
            Blender->output_height = 0;
            return;
        }
        const int usable_width = mlv->width - mlv->crop_left - mlv->crop_right;
        const int usable_height = mlv->height - mlv->crop_top - mlv->crop_bottom;
        if (mlv->feather_left > usable_width
            || mlv->feather_right > usable_width - mlv->feather_left
            || mlv->feather_bottom > usable_height
            || mlv->feather_top > usable_height - mlv->feather_bottom) {
            free(Blender->blended_output);
            Blender->blended_output = NULL;
            Blender->output_width = 0;
            Blender->output_height = 0;
            return;
        }

        /* Left edge */
        int64_t x1 = (int64_t)mlv->offset_x + mlv->crop_left;
        /* Right edge */
        int64_t x2 = (int64_t)mlv->offset_x + mlv->width - 1 - mlv->crop_right;
        /* Bottom edge */
        int64_t y1 = (int64_t)mlv->offset_y + mlv->crop_bottom;
        /* Top edge */
        int64_t y2 = (int64_t)mlv->offset_y + mlv->height - 1 - mlv->crop_top;

        if (x1 < min_x) min_x = x1;
        if (x2 > max_x) max_x = x2;
        if (y1 < min_y) min_y = y1;
        if (y2 > max_y) max_y = y2;
    }

    uint64_t wide_output_width = (uint64_t)(max_x - min_x) + 1u;
    uint64_t wide_output_height = (uint64_t)(max_y - min_y) + 1u;
    if (wide_output_width > (uint64_t)INT_MAX || wide_output_height > (uint64_t)INT_MAX
        || wide_output_width > SIZE_MAX / wide_output_height
        || (size_t)(wide_output_width * wide_output_height) > SIZE_MAX / sizeof(float)) {
        free(Blender->blended_output);
        Blender->blended_output = NULL;
        Blender->output_width = 0;
        Blender->output_height = 0;
        return;
    }

    size_t output_width = (size_t)wide_output_width;
    size_t output_height = (size_t)wide_output_height;
    float * blended_output = calloc(output_width * output_height, sizeof(float));
    if (!blended_output) {
        free(Blender->blended_output);
        Blender->blended_output = NULL;
        Blender->output_width = 0;
        Blender->output_height = 0;
        return;
    }
    free(Blender->blended_output);
    Blender->blended_output = blended_output;
    Blender->output_width = (int)output_width;
    Blender->output_height = (int)output_height;

    /***************************** BEGIN BLENDING *****************************/

    for (int i = 0; i < Blender->num_mlvs; ++i)
    {
        MLVBlender_mlv_t * mlv = Blender->mlvs + i;

        if (!mlv->visible) continue;

        size_t frame_size = (size_t)mlv->width * (size_t)mlv->height;
        uint16_t * frame_data = malloc(frame_size * sizeof(uint16_t));
        if (!frame_data || getMlvFrames(mlv->mlv) == 0) {
            free(frame_data);
            free(Blender->blended_output);
            Blender->blended_output = NULL;
            Blender->output_width = 0;
            Blender->output_height = 0;
            return;
        }

        /* If we are past the end of the MLV just keep using it's last frame */
        uint64_t get_frame;
        if (getMlvFrames(mlv->mlv) > FrameIndex) {
            get_frame = FrameIndex;
        } else {
            get_frame = getMlvFrames(mlv->mlv)-1;
        }
        
        /* Get frame from index B4 if it fails */
        if (getMlvRawFrameUint16(mlv->mlv, get_frame, frame_data) != 0) {
            free(frame_data);
            free(Blender->blended_output);
            Blender->blended_output = NULL;
            Blender->output_width = 0;
            Blender->output_height = 0;
            return;
        }

        int32_t black_level = getMlvBlackLevel(mlv->mlv);
        for (size_t j = 0; j < frame_size; ++j)
        {
            int32_t new_val = (int32_t)frame_data[j] - black_level;
            if (new_val < 0) new_val = 0;
            frame_data[j] = new_val;
        }

        /* Exposure and correction factor to get in to 0-1 range */
        float exposure = mlv->exposure / pow(2.0, getMlvBitdepth(mlv->mlv));

        /**************************** PUT THE IMAGE ***************************/

        if (mlv->difference_blending)
        {
            for (size_t y = mlv->crop_bottom; y < mlv->height-mlv->crop_top; ++y)
            {
                size_t index_src = mlv->width * (mlv->height-1-y); /* Index for row Y */
                size_t index_dst = output_width * (y+mlv->offset_y-min_y) + mlv->offset_x-min_x; /* Index for row Y */
                for (size_t x = mlv->crop_left; x < mlv->width-mlv->crop_right; ++x)
                {
                    float result = Blender->blended_output[index_dst+x] - (float)frame_data[index_src+x]*exposure;
                    Blender->blended_output[index_dst+x] = (result > 0) ? result : -result;
                }
            }
        }
        else
        {
            /* Feather the bottom part */
            for (size_t y = mlv->crop_bottom; y < mlv->crop_bottom+mlv->feather_bottom; ++y)
            {
                size_t index_src = mlv->width * (mlv->height-1-y); /* Index for row Y */
                size_t index_dst = output_width * (y+mlv->offset_y-min_y) + mlv->offset_x-min_x; /* Index for row Y */
                float alpha = ((float)(y-mlv->crop_bottom)) / ((float)mlv->feather_bottom);
                float ialpha = 1.0f - alpha;

                for (size_t x = mlv->crop_left; x < mlv->crop_left+mlv->feather_left; ++x)
                {
                    float alpha2 = ((float)(x-mlv->crop_left)) / ((float)mlv->feather_left)*alpha;
                    float ialpha2 = 1.0f - alpha2;
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha2 + Blender->blended_output[index_dst+x]*ialpha2;
                }
                for (size_t x = mlv->crop_left+mlv->feather_left; x < mlv->width-mlv->crop_right-mlv->feather_right; ++x)
                {
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha + Blender->blended_output[index_dst+x]*ialpha;
                }
                for (size_t x = mlv->width-mlv->crop_right-mlv->feather_right; x < mlv->width-mlv->crop_right; ++x)
                {
                    float alpha2 = (1.0f - ((float)(x-(mlv->width-mlv->crop_right-mlv->feather_right))) / ((float)mlv->feather_right)) * alpha;
                    float ialpha2 = 1.0f - alpha2;
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha2 + Blender->blended_output[index_dst+x]*ialpha2;
                }
            }

            for (size_t y = mlv->crop_bottom+mlv->feather_bottom; y < mlv->height-mlv->crop_top-mlv->feather_top; ++y)
            {
                size_t index_src = mlv->width * (mlv->height-1-y); /* Index for row Y */
                size_t index_dst = output_width * (y+mlv->offset_y-min_y) + mlv->offset_x-min_x; /* Index for row Y */

                for (size_t x = mlv->crop_left; x < mlv->crop_left+mlv->feather_left; ++x)
                {
                    float alpha = ((float)(x-mlv->crop_left)) / ((float)mlv->feather_left);
                    float ialpha = 1.0f - alpha;
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha + Blender->blended_output[index_dst+x]*ialpha;
                }
                for (size_t x = mlv->crop_left+mlv->feather_left; x < mlv->width-mlv->crop_right-mlv->feather_right; ++x)
                {
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure;
                }
                for (size_t x = mlv->width-mlv->crop_right-mlv->feather_right; x < mlv->width-mlv->crop_right; ++x)
                {
                    float alpha = 1.0 - ((float)(x-(mlv->width-mlv->crop_right-mlv->feather_right))) / ((float)mlv->feather_right);
                    float ialpha = 1.0f - alpha;
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha + Blender->blended_output[index_dst+x]*ialpha;
                }
            }

            /* Feather the top part */
            for (size_t y = mlv->height-mlv->crop_top-mlv->feather_top; y < mlv->height-mlv->crop_top; ++y)
            {
                size_t index_src = mlv->width * (mlv->height-1-y); /* Index for row Y */
                size_t index_dst = output_width * (y+mlv->offset_y-min_y) + mlv->offset_x-min_x; /* Index for row Y */
                float alpha = 1.0f - ((float)(y-(mlv->height-mlv->crop_top-mlv->feather_top))) / ((float)mlv->feather_top);
                float ialpha = 1.0f - alpha;

                for (size_t x = mlv->crop_left; x < mlv->crop_left+mlv->feather_left; ++x)
                {
                    float alpha2 = ((float)(x-mlv->crop_left)) / ((float)mlv->feather_left)*alpha;
                    float ialpha2 = 1.0f - alpha2;
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha2 + Blender->blended_output[index_dst+x]*ialpha2;
                }
                for (size_t x = mlv->crop_left+mlv->feather_left; x < mlv->width-mlv->crop_right-mlv->feather_right; ++x)
                {
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha + Blender->blended_output[index_dst+x]*ialpha;
                }
                for (size_t x = mlv->width-mlv->crop_right-mlv->feather_right; x < mlv->width-mlv->crop_right; ++x)
                {
                    float alpha2 = (1.0f - ((float)(x-(mlv->width-mlv->crop_right-mlv->feather_right))) / ((float)mlv->feather_right)) * alpha;
                    float ialpha2 = 1.0f - alpha2;
                    Blender->blended_output[index_dst+x] = frame_data[index_src+x]*exposure*alpha2 + Blender->blended_output[index_dst+x]*ialpha2;
                }
            }
        }

        free(frame_data);
    }
}

float * MLVBlenderGetOutput(MLVBlender_t * Blender)
{
    return Blender->blended_output;
}

int MLVBlenderGetOutputWidth(MLVBlender_t * Blender)
{
    return Blender->output_width;
}

int MLVBlenderGetOutputHeight(MLVBlender_t * Blender)
{
    return Blender->output_height;
}

int MLVBlenderExportMLV(MLVBlender_t * Blender, const char * OutputPath)
{
    if (!Blender || !OutputPath || Blender->num_mlvs <= 0 || !Blender->mlvs) return 1;
    /* Set length to longest vid */
    uint64_t longest_vid = Blender->mlvs[0].num_frames;
    int longest_vid_index = 0;
    for (int i = 0; i < Blender->num_mlvs; ++i)
    {
        if (Blender->mlvs[i].num_frames > longest_vid)
        {
            longest_vid = Blender->mlvs[i].num_frames;
            longest_vid_index = i;
        }
    }

    uint32_t frame_start = 0;
    uint32_t frame_end = 0;
    if (!MLVBlenderInclusiveFrameRange(longest_vid, &frame_start, &frame_end)) return 1;

    mlvObject_t * mlv_object = initMlvObjectWithClip(Blender->mlvs[longest_vid_index].file_name, MLV_OPEN_FULL, NULL, NULL);

    if (!mlv_object) return 1;
    FILE * mlv_output_file = fopen(OutputPath, "wb");
    if (!mlv_output_file) { freeMlvObject(mlv_object); return 1; }

    MLVBlenderBlend(Blender, 0);

    char error[256] = { 0 };

    /* Round output width down to a multiple of 8, and height 2 */
    size_t output_width = MLVBlenderGetOutputWidth(Blender);
    size_t output_height = MLVBlenderGetOutputHeight(Blender);
    size_t result_width = output_width - (output_width % 8);
    size_t result_height = output_height - (output_height % 2);
    if (result_width == 0 || result_height == 0
        || result_width > (size_t)UINT16_MAX || result_height > (size_t)UINT16_MAX
        || result_width > SIZE_MAX / result_height) {
        freeMlvObject(mlv_object);
        fclose(mlv_output_file);
        remove(OutputPath);
        return 1;
    }
    size_t frame_size = result_width * result_height;
    size_t compressed_capacity = 0;
    if (frame_size > SIZE_MAX / sizeof(uint16_t)
        || !dng_lj92_output_capacity((int)result_width, (int)result_height,
                                     &compressed_capacity)) {
        freeMlvObject(mlv_object);
        fclose(mlv_output_file);
        remove(OutputPath);
        return 1;
    }

    /* Set fake dimensions inside MLV object and save headers */
    getMlvWidth(mlv_object) = result_width;
    getMlvHeight(mlv_object) = result_height;
    /* Always 14 bit export */
    int bitdepth = 14;
    /* Use full range in 16 bit */
    if (bitdepth == 16) {
        getMlvBlackLevel(mlv_object) = 1;
        getMlvWhiteLevel(mlv_object) = 65534;
    }
    else {
        getMlvBlackLevel(mlv_object) = (int) ( (float)getMlvBlackLevel(mlv_object) * pow(2.0, bitdepth-getMlvBitdepth(mlv_object)) );
        if (getMlvBlackLevel(mlv_object) < 0) getMlvBlackLevel(mlv_object) = 0;
        if (getMlvBlackLevel(mlv_object) > 16382) getMlvBlackLevel(mlv_object) = 16382;
        getMlvWhiteLevel(mlv_object) = (1 << bitdepth) - 1;
    }
    getMlvBitdepth(mlv_object) = bitdepth;

    int export_failed = 0;
    /* Annoying */
    if (isMlvCompressed(mlv_object))
        export_failed = saveMlvHeaders(mlv_object, mlv_output_file, 0, MLV_FAST_PASS,
                                       frame_start, frame_end, "MLVStitcher", error);
    else
        export_failed = saveMlvHeaders(mlv_object, mlv_output_file, 0, MLV_LJ92,
                                       frame_start, frame_end, "MLVStitcher", error);

    if (export_failed) {
        freeMlvObject(mlv_object);
        fclose(mlv_output_file);
        remove(OutputPath);
        return 1;
    }

    uint16_t * buffer16 = malloc(sizeof(uint16_t) * frame_size);
    uint8_t * buffer_compressed = malloc(compressed_capacity);
    if (!buffer16 || !buffer_compressed) {
        free(buffer16);
        free(buffer_compressed);
        freeMlvObject(mlv_object);
        fclose(mlv_output_file);
        remove(OutputPath);
        return 1;
    }

    for (uint64_t f = 0; f < longest_vid; ++f)
    {
        printf("Exporting frame %" PRIu64 "/%" PRIu64 "\n", f, longest_vid);
        if (f != 0) MLVBlenderBlend(Blender, f);
        if (!Blender->blended_output
            || Blender->output_width != (int)output_width
            || Blender->output_height != (int)output_height) {
            export_failed = 1;
            break;
        }

        int black_level = getMlvBlackLevel(mlv_object);

        /* Flip to correct orientation + crop to valid mlv dimensions (result_height and result_width) */
        for (size_t y = 0; y < result_height; ++y)
        {
            float * src_pix = Blender->blended_output + y * output_width;
            uint16_t * dst_pix = buffer16 + (result_height - 1u - y) * result_width;

            for (size_t x = 0; x < result_width; ++x)
            {
                dst_pix[x] = MLVBlenderQuantize14(src_pix[x], black_level);
            }
        }

        size_t frame_size_compressed = 0;
        int ret = dng_compress_image((uint16_t *)buffer_compressed,
                                     compressed_capacity,
                                     buffer16,
                                     &frame_size_compressed,
                                     result_width,
                                     result_height,
                                     bitdepth);
        if (ret != 0 || frame_size_compressed == 0
            || frame_size_compressed > compressed_capacity) {
            export_failed = 1;
            break;
        }

        /* Write frame */
        mlv_vidf_hdr_t vidf_hdr = { 0 };
        int chunk = mlv_object->video_index[f].chunk_num;
        uint64_t block_offset = mlv_object->video_index[f].block_offset;
        /* read VIDF block header */
        if (file_set_pos(mlv_object->file[chunk], block_offset, SEEK_SET) != 0
            || fread(&vidf_hdr, sizeof(mlv_vidf_hdr_t), 1, mlv_object->file[chunk]) != 1) {
            export_failed = 1;
            break;
        }
        vidf_hdr.frameSpace = 0;
        vidf_hdr.blockSize = sizeof(mlv_vidf_hdr_t) + frame_size_compressed;
        if (fwrite(&vidf_hdr, sizeof(mlv_vidf_hdr_t), 1, mlv_output_file) != 1
            || fwrite(buffer_compressed, frame_size_compressed, 1, mlv_output_file) != 1) {
            export_failed = 1;
            break;
        }
    }

    free(buffer16);
    free(buffer_compressed);

    printf("blacklevel = %i, whitelevel = %i\n", getMlvBlackLevel(mlv_object), getMlvWhiteLevel(mlv_object));

    getMlvWidth(mlv_object) = Blender->mlvs[0].width;
    getMlvHeight(mlv_object) = Blender->mlvs[0].height;

    freeMlvObject(mlv_object);

    if (fclose(mlv_output_file) != 0) export_failed = 1;

    if (export_failed) {
        remove(OutputPath);
        return 1;
    }

    puts("yes");
    return 0;
}

/* Exposure */
void MLVBlenderSetMLVExposure(MLVBlender_t * Blender, int MLVIndex, float ExposureValue) {
    Blender->mlvs[MLVIndex].exposure = ExposureValue;
} float MLVBlenderGetMLVExposure(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].exposure;
}

/* Set offsets */
void MLVBlenderSetMLVOffsetX(MLVBlender_t * Blender, int MLVIndex, int Offset) {
    Blender->mlvs[MLVIndex].offset_x = Offset;
} void MLVBlenderSetMLVOffsetY(MLVBlender_t * Blender, int MLVIndex, int Offset) {
    Blender->mlvs[MLVIndex].offset_y = Offset;
}
/* Get offsets */
int MLVBlenderGetMLVOffsetX(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].offset_x;
} int MLVBlenderGetMLVOffsetY(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].offset_y;
}

/* Set crops */
void MLVBlenderSetMLVCropRight(MLVBlender_t * Blender, int MLVIndex, int Crop) {
    Blender->mlvs[MLVIndex].crop_right = Crop;
} void MLVBlenderSetMLVCropLeft(MLVBlender_t * Blender, int MLVIndex, int Crop) {
    Blender->mlvs[MLVIndex].crop_left = Crop;
} void MLVBlenderSetMLVCropTop(MLVBlender_t * Blender, int MLVIndex, int Crop) {
    Blender->mlvs[MLVIndex].crop_top = Crop;
} void MLVBlenderSetMLVCropBottom(MLVBlender_t * Blender, int MLVIndex, int Crop) {
    Blender->mlvs[MLVIndex].crop_bottom = Crop;
}
/* Get crops */
int MLVBlenderGetMLVCropRight(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].crop_right;
} int MLVBlenderGetMLVCropLeft(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].crop_left;
} int MLVBlenderGetMLVCropTop(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].crop_top;
} int MLVBlenderGetMLVCropBottom(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].crop_bottom;
}

/* Set feather */
void MLVBlenderSetMLVFeatherRight(MLVBlender_t * Blender, int MLVIndex, int Feather) {
    Blender->mlvs[MLVIndex].feather_right = Feather;
} void MLVBlenderSetMLVFeatherLeft(MLVBlender_t * Blender, int MLVIndex, int Feather) {
    Blender->mlvs[MLVIndex].feather_left = Feather;
} void MLVBlenderSetMLVFeatherTop(MLVBlender_t * Blender, int MLVIndex, int Feather) {
    Blender->mlvs[MLVIndex].feather_top = Feather;
} void MLVBlenderSetMLVFeatherBottom(MLVBlender_t * Blender, int MLVIndex, int Feather) {
    Blender->mlvs[MLVIndex].feather_bottom = Feather;
}
/* Get feather */
int MLVBlenderGetMLVFeatherRight(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].feather_right;
} int MLVBlenderGetMLVFeatherLeft(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].feather_left;
} int MLVBlenderGetMLVFeatherTop(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].feather_top;
} int MLVBlenderGetMLVFeatherBottom(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].feather_bottom;
}

/* Visibility */
void MLVBlenderSetMLVVisible(MLVBlender_t * Blender, int MLVIndex, int Visible) {
    Blender->mlvs[MLVIndex].visible = Visible;
} int MLVBlenderGetMLVVisible(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].visible;
}


void MLVBlenderSetMLVDifferenceBlending(MLVBlender_t * Blender, int MLVIndex, int UseDifference) {
    Blender->mlvs[MLVIndex].difference_blending = UseDifference;
} int MLVBlenderGetMLVDifferenceBlending(MLVBlender_t * Blender, int MLVIndex) {
    return Blender->mlvs[MLVIndex].difference_blending;
}
