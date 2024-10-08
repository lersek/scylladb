/*
 * Copyright (C) 2024-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#include <cstdint>

#include <seastar/core/bitops.hh>
#include <seastar/core/fstream.hh>
#include <seastar/core/future.hh>
#include <seastar/core/temporary_buffer.hh>

#include "types.hh"
#include "exceptions.hh"
#include "checksum_utils.hh"

namespace sstables {

extern logging::logger sstlog;

// File data source implementation for SSTables with attached checksum
// data and no compression
template <ChecksumUtils ChecksumType>
class checksummed_file_data_source_impl : public data_source_impl {
    std::optional<input_stream<char>> _input_stream;
    const checksum& _checksum;
    uint64_t _chunk_size_trailing_zeros;
    uint64_t _underlying_pos;
    uint64_t _pos;
    uint64_t _beg_pos;
    uint64_t _end_pos;
public:
    checksummed_file_data_source_impl(file f, uint64_t file_len,
                const checksum& checksum, uint64_t pos, size_t len,
                file_input_stream_options options)
            : _checksum(checksum)
            , _pos(pos)
            , _beg_pos(pos)
            , _end_pos(pos + len)
    {
        // _beg_pos and _end_pos specify positions in the stream.
        // These are not necessarily aligned on chunk boundaries.
        // To be able to verify the checksums, we need to translate
        // them into a range of chunks that contain the given
        // address range, and open a file input stream to read that
        // range. The _underlying_pos always points to the current
        // chunk-aligned position of the file input stream.
        auto chunk_size = checksum.chunk_size;
        if (chunk_size == 0 || (chunk_size & (chunk_size - 1)) != 0) {
            on_internal_error(sstlog, format("Invalid chunk size: {}", chunk_size));
        }
        _chunk_size_trailing_zeros = count_trailing_zeros(chunk_size);
        if (_pos > file_len) {
            on_internal_error(sstlog, "attempt to read beyond end");
        }
        if (len == 0 || _pos == file_len) {
            // Nothing to read
            _end_pos = _pos;
            return;
        }
        if (len > file_len - _pos) {
            _end_pos = file_len;
        }
        auto start = _beg_pos & ~(chunk_size - 1);
        auto end = (_end_pos & ~(chunk_size - 1)) + chunk_size;
        _input_stream = make_file_input_stream(std::move(f), start, end - start, std::move(options));
        _underlying_pos = start;
    }

    virtual future<temporary_buffer<char>> get() override {
        auto chunk_size = _checksum.chunk_size;
        if (_pos >= _end_pos) {
            return make_ready_future<temporary_buffer<char>>();
        }
        // Read the next chunk. We need to skip part of the first
        // chunk, but then continue to read from beginning of chunks.
        // Also, we need to take into account that the last chunk can
        // be smaller than `chunk_size`.
        if (_pos != _beg_pos && (_pos & (chunk_size - 1)) != 0) {
            throw std::runtime_error(format("Checksummed reader not aligned to chunk boundary: pos={}, chunk_size={}", _pos, chunk_size));
        }
        return _input_stream->read_exactly(chunk_size).then([this, chunk_size](temporary_buffer<char> buf) {
            auto expected_checksum = _checksum.checksums[_pos >> _chunk_size_trailing_zeros];
            auto actual_checksum = ChecksumType::checksum(buf.get(), buf.size());
            if (expected_checksum != actual_checksum) {
                throw sstables::malformed_sstable_exception(format("Checksummed chunk of size {} at file offset {} failed checksum: expected={}, actual={}", buf.size(), _underlying_pos, expected_checksum, actual_checksum));
            }
            buf.trim_front(_pos & (chunk_size - 1));
            _pos += buf.size();
            _underlying_pos += chunk_size;
            return buf;
        });
    }

    virtual future<> close() override {
        if (!_input_stream) {
            return make_ready_future<>();
        }
        return _input_stream->close();
    }

    virtual future<temporary_buffer<char>> skip(uint64_t n) override {
        auto chunk_size = _checksum.chunk_size;
        if (_pos + n > _end_pos) {
            on_internal_error(sstlog, format("Skipping over the end position is disallowed: current pos={}, end pos={}, skip len={}", _pos, _end_pos, n));
        }
        _pos += n;
        if (_pos == _end_pos) {
            return make_ready_future<temporary_buffer<char>>();
        }
        auto underlying_n = (_pos & ~(chunk_size - 1)) - _underlying_pos;
        _beg_pos = _pos;
        _underlying_pos += underlying_n;
        return _input_stream->skip(underlying_n).then([] {
            return make_ready_future<temporary_buffer<char>>();
        });
    }
};

template <ChecksumUtils ChecksumType>
class checksummed_file_data_source : public data_source {
public:
    checksummed_file_data_source(file f, uint64_t file_len, const checksum& checksum,
            uint64_t offset, size_t len, file_input_stream_options options)
        : data_source(std::make_unique<checksummed_file_data_source_impl<ChecksumType>>(
                std::move(f), file_len, checksum, offset, len, std::move(options)))
    {}
};

template <ChecksumUtils ChecksumType>
inline input_stream<char> make_checksummed_file_input_stream(
        file f, uint64_t file_len, const checksum& checksum, uint64_t offset,
        size_t len, file_input_stream_options options)
{
    return input_stream<char>(checksummed_file_data_source<ChecksumType>(
        std::move(f), file_len, checksum, offset, len, std::move(options)));
}

input_stream<char> make_checksummed_file_k_l_format_input_stream(
        file f, uint64_t file_len, const checksum& checksum, uint64_t offset,
        size_t len, file_input_stream_options options)
{
    return make_checksummed_file_input_stream<adler32_utils>(std::move(f), file_len, checksum, offset, len, std::move(options));
}

input_stream<char> make_checksummed_file_m_format_input_stream(
        file f, uint64_t file_len, const checksum& checksum, uint64_t offset,
        size_t len, file_input_stream_options options)
{
    return make_checksummed_file_input_stream<crc32_utils>(std::move(f), file_len, checksum, offset, len, std::move(options));
}

}
